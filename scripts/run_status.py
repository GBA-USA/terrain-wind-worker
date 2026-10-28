#!/usr/bin/env python3
"""Publish workflow run status objects to R2 (latest + rolling history)."""

import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

HISTORY_LIMIT = 40
SENSITIVE_KEYS = frozenset(
    {
        "bbox",
        "url",
        "urls",
        "token",
        "authorization",
        "areas_url",
        "club",
        "club_id",
    }
)


def log(message):
    print(message, flush=True)


def iso_now():
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def status_root_dir(workflow):
    test_mode = os.environ.get("TEST_MODE", "").strip().lower() == "true"
    if workflow == "terrain-wind":
        prefix = os.environ.get("TERRAIN_WIND_PREFIX", "wind").strip() or "wind"
        if test_mode or prefix != "wind":
            return "status-test"
    elif test_mode:
        return "status-test"
    return "status"


def github_field(name, default):
    raw = os.environ.get(name, "").strip()
    return raw if raw else default


def timeout_seconds():
    raw = os.environ.get("RUN_TIMEOUT_S", "3600").strip()
    try:
        return int(raw)
    except ValueError:
        return 3600


def sanitize_payload(payload):
    """Drop keys that must never appear in public status objects."""

    def walk(value):
        if isinstance(value, dict):
            cleaned = {}
            for key, item in value.items():
                if key in SENSITIVE_KEYS:
                    continue
                cleaned[key] = walk(item)
            return cleaned
        if isinstance(value, list):
            return [walk(item) for item in value]
        return value

    return walk(payload)


def run_identity():
    run_id = github_field("GITHUB_RUN_ID", "local")
    attempt_raw = github_field("GITHUB_RUN_ATTEMPT", "1")
    try:
        run_attempt = int(attempt_raw)
    except ValueError:
        run_attempt = 1
    event = github_field("GITHUB_EVENT_NAME", "local")
    return run_id, run_attempt, event


class StatusStore:
    def read_json(self, key):
        raise NotImplementedError

    def write_json(self, key, payload, cache_control="no-cache"):
        raise NotImplementedError


class MemoryStatusStore(StatusStore):
    def __init__(self):
        self.objects = {}

    def read_json(self, key):
        raw = self.objects.get(key)
        if raw is None:
            return None
        return json.loads(raw)

    def write_json(self, key, payload, cache_control="no-cache"):
        self.objects[key] = json.dumps(payload, separators=(",", ":"), sort_keys=True)


class R2StatusStore(StatusStore):
    def __init__(self):
        from terrain_wind import r2_env, upload_file

        self._r2_env = r2_env
        self._upload_file = upload_file
        self._env = None
        self._endpoint = None
        self._bucket = None

    def _ensure(self):
        if self._env is None:
            self._env, self._endpoint, self._bucket = self._r2_env()

    def read_json(self, key):
        self._ensure()
        import subprocess

        command = [
            "aws",
            "s3",
            "cp",
            f"s3://{self._bucket}/{key}",
            "-",
            "--endpoint-url",
            self._endpoint,
            "--region",
            "auto",
            "--only-show-errors",
        ]
        completed = subprocess.run(
            command,
            capture_output=True,
            env=self._env,
            timeout=120,
        )
        if completed.returncode != 0:
            return None
        try:
            return json.loads(completed.stdout.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    def write_json(self, key, payload, cache_control="no-cache"):
        self._ensure()
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
            handle.write(body)
            path = Path(handle.name)
        try:
            self._upload_file(
                path,
                key,
                self._env,
                self._endpoint,
                self._bucket,
                False,
                content_type="application/json",
                cache_control=cache_control,
            )
        finally:
            path.unlink(missing_ok=True)


def store_for_env():
    if os.environ.get("RUN_STATUS_MEMORY", "").strip().lower() == "true":
        return MemoryStatusStore()
    if os.environ.get("TEST_MODE", "").strip().lower() == "true" and not _r2_configured():
        return MemoryStatusStore()
    return R2StatusStore()


def _r2_configured():
    for name in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
        if not os.environ.get(name, "").strip():
            return False
    return True


def history_key(workflow):
    root = status_root_dir(workflow)
    return f"{root}/{workflow}/history.json"


def latest_key(workflow):
    root = status_root_dir(workflow)
    return f"{root}/{workflow}/latest.json"


def merge_history(existing, entry):
    history = []
    if isinstance(existing, dict) and isinstance(existing.get("entries"), list):
        history = list(existing["entries"])
    run_id = entry.get("run_id")
    run_attempt = entry.get("run_attempt")
    history = [
        item
        for item in history
        if not (item.get("run_id") == run_id and item.get("run_attempt") == run_attempt)
    ]
    history.insert(0, entry)
    history = history[:HISTORY_LIMIT]
    return {"v": 1, "entries": history}


class RunStatusReporter:
    def __init__(self, workflow, store=None):
        self.workflow = workflow
        self.store = store or store_for_env()
        self.started_wall = iso_now()
        self.started_monotonic = time.monotonic()
        self.run_id, self.run_attempt, self.event = run_identity()
        self._base = None

    def _base_payload(self):
        if self._base is None:
            self._base = sanitize_payload(
                {
                    "v": 1,
                    "workflow": self.workflow,
                    "run_id": self.run_id,
                    "run_attempt": self.run_attempt,
                    "event": self.event,
                    "started": self.started_wall,
                    "finished": None,
                    "timeout_s": timeout_seconds(),
                    "conclusion": "running",
                }
            )
        return dict(self._base)

    def _write_latest(self, payload):
        self.store.write_json(latest_key(self.workflow), payload)

    def _write_history(self, entry):
        existing = self.store.read_json(history_key(self.workflow))
        merged = merge_history(existing, entry)
        self.store.write_json(history_key(self.workflow), merged)

    def _safe_write(self, writer):
        try:
            writer()
        except Exception as exc:
            log(f"run status write failed: {type(exc).__name__}")

    def start(self):
        payload = self._base_payload()

        def do_write():
            self._write_latest(payload)
            self._write_history(payload)

        self._safe_write(do_write)

    def finish(self, conclusion, **fields):
        runtime_s = time.monotonic() - self.started_monotonic
        payload = self._base_payload()
        payload.update(fields)
        payload["finished"] = iso_now()
        payload["runtime_s"] = round(runtime_s, 3)
        payload["conclusion"] = conclusion
        payload = sanitize_payload(payload)

        def do_write():
            self._write_latest(payload)
            self._write_history(payload)

        self._safe_write(do_write)

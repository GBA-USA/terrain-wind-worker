import importlib.util
import json
import os
import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
WORKFLOW = ROOT / ".github" / "workflows" / "terrain-wind.yml"

SPEC = importlib.util.spec_from_file_location("terrain_wind", SCRIPTS / "terrain_wind.py")
terrain_wind = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(terrain_wind)


def fake_areas(count):
    # Shuffled insertion order so sorting by key is observable.
    keys = [f"t{index:03d}_-832" for index in range(count)]
    keys = keys[1::2] + keys[0::2]
    return [{"key": key, "bbox": [-80.0, 38.0, -79.9, 38.1], "cell_m": 100} for key in keys]


class BatchSplitTests(unittest.TestCase):
    def test_fifteen_tiles_make_eight_batches(self):
        batches = terrain_wind.batch_areas(fake_areas(15))
        self.assertEqual(len(batches), 8)
        self.assertEqual([len(group) for group in batches], [2] * 7 + [1])

    def test_sixty_tiles_make_thirty_batches(self):
        self.assertEqual(len(terrain_wind.batch_areas(fake_areas(60))), 30)

    def test_no_tiles_make_no_batches(self):
        self.assertEqual(terrain_wind.batch_areas([]), [])

    def test_batches_are_sorted_by_key_and_cover_every_tile_once(self):
        areas = fake_areas(15)
        batches = terrain_wind.batch_areas(areas)
        flat = [area["key"] for group in batches for area in group]
        self.assertEqual(flat, sorted(area["key"] for area in areas))

    def test_cache_key_is_stable_and_sensitive_to_bbox_and_cell(self):
        group = fake_areas(2)
        base = terrain_wind.batch_cache_key(group)
        self.assertEqual(base, terrain_wind.batch_cache_key(list(reversed(group))))
        moved = [dict(group[0], bbox=[-80.0, 38.0, -79.9, 38.2]), group[1]]
        coarse = [dict(group[0], cell_m=200), group[1]]
        self.assertNotEqual(base, terrain_wind.batch_cache_key(moved))
        self.assertNotEqual(base, terrain_wind.batch_cache_key(coarse))

    def test_plan_emits_batches_and_pins_cycle(self):
        cycle = datetime(2026, 10, 30, 9, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            areas_file = Path(tmp) / "areas.json"
            out_file = Path(tmp) / "out.txt"
            areas_file.write_text(
                json.dumps({"areas": fake_areas(5), "wind_meta": {"tiles_capped": ["tx"], "areas_coarse": 1}}),
                encoding="utf-8",
            )
            env = {
                "AREAS_FILE": str(areas_file),
                "GITHUB_OUTPUT": str(out_file),
                "RUN_STATUS_MEMORY": "true",
            }
            with mock.patch.dict(os.environ, env), mock.patch.object(
                terrain_wind, "latest_hrrr_cycle", return_value=cycle
            ):
                terrain_wind.plan()
            lines = dict(line.split("=", 1) for line in out_file.read_text().splitlines())
        self.assertEqual(json.loads(lines["batch_indexes"]), [0, 1, 2])
        batches = json.loads(lines["batches"])
        self.assertEqual(batches[0]["keys"], ["t000_-832", "t001_-832"])
        self.assertEqual(batches[2]["keys"], ["t004_-832"])
        self.assertEqual(lines["hrrr_cycle"], "2026-10-30T09:00:00Z")
        self.assertEqual(json.loads(lines["meta"])["tiles_capped"], ["tx"])
        self.assertIn("started", lines)

    def test_batch_command_writes_only_that_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            areas_file = Path(tmp) / "areas.json"
            batch_file = Path(tmp) / "batch.json"
            out_file = Path(tmp) / "out.txt"
            areas_file.write_text(json.dumps({"areas": fake_areas(5)}), encoding="utf-8")
            env = {
                "AREAS_FILE": str(areas_file),
                "BATCH_FILE": str(batch_file),
                "BATCH_INDEX": "1",
                "GITHUB_OUTPUT": str(out_file),
            }
            with mock.patch.dict(os.environ, env):
                terrain_wind.batch()
            written = json.loads(batch_file.read_text(encoding="utf-8"))
            self.assertEqual([a["key"] for a in written["areas"]], ["t002_-832", "t003_-832"])
            self.assertIn("cache_key=", out_file.read_text())
            with mock.patch.dict(os.environ, dict(env, BATCH_INDEX="3")):
                with self.assertRaises(SystemExit):
                    terrain_wind.batch()


class HrrrCycleOverrideTests(unittest.TestCase):
    def test_override_is_used_without_network(self):
        with mock.patch.dict(os.environ, {"TERRAIN_WIND_HRRR_CYCLE": "2026-10-30T06:00:00Z"}), mock.patch.object(
            terrain_wind, "latest_hrrr_cycle", side_effect=AssertionError("must not look up")
        ):
            cycle = terrain_wind.resolve_hrrr_cycle()
        self.assertEqual(cycle, datetime(2026, 10, 30, 6, tzinfo=timezone.utc))
        self.assertEqual(terrain_wind.iso(cycle), "2026-10-30T06:00:00Z")

    def test_unset_or_blank_falls_back_to_latest(self):
        latest = datetime(2026, 10, 30, 8, tzinfo=timezone.utc)
        for value in (None, "", "  "):
            env = {} if value is None else {"TERRAIN_WIND_HRRR_CYCLE": value}
            with mock.patch.dict(os.environ, env), mock.patch.object(
                terrain_wind, "latest_hrrr_cycle", return_value=latest
            ):
                os.environ.pop("TERRAIN_WIND_HRRR_CYCLE", None) if value is None else None
                self.assertEqual(terrain_wind.resolve_hrrr_cycle(), latest)

    def test_malformed_override_is_an_error(self):
        with mock.patch.dict(os.environ, {"TERRAIN_WIND_HRRR_CYCLE": "yesterday"}):
            with self.assertRaises(RuntimeError):
                terrain_wind.resolve_hrrr_cycle()


class StatusSkipAndFinalizeTests(unittest.TestCase):
    def setUp(self):
        self.run_status = terrain_wind.import_run_status()
        self.store = self.run_status.MemoryStatusStore()
        patcher = mock.patch.object(self.run_status, "store_for_env", return_value=self.store)
        patcher.start()
        self.addCleanup(patcher.stop)

    def finalize(self, planned, results, plan_result="success", cycle="2026-10-30T06:00:00Z"):
        batches = [{"batch": i, "keys": planned[i : i + 2]} for i in range(0, len(planned), 2)]
        with tempfile.TemporaryDirectory() as tmp:
            for index, result in enumerate(results):
                (Path(tmp) / f"result-{index}.json").write_text(json.dumps(result), encoding="utf-8")
            env = {
                "PLAN_BATCHES": json.dumps(batches),
                "PLAN_META": json.dumps({"areas_oversized": 2, "areas_coarse": 1, "tiles_capped": ["tz"]}),
                "PLAN_RESULT": plan_result,
                "HRRR_CYCLE": cycle,
                "RESULTS_DIR": tmp,
                "RUN_STATUS_STARTED": "2026-10-30T06:10:00Z",
                "TERRAIN_WIND_PREFIX": "wind",
                "TEST_MODE": "false",
            }
            with mock.patch.dict(os.environ, env):
                code = 0
                try:
                    terrain_wind.finalize()
                except SystemExit as exc:
                    code = exc.code
        latest = self.store.read_json(self.run_status.latest_key("terrain-wind"))
        history = self.store.read_json(self.run_status.history_key("terrain-wind"))
        return code, latest, history

    def test_skip_flag_suppresses_start_and_finish(self):
        with mock.patch.dict(os.environ, {"RUN_STATUS_SKIP": "true"}):
            reporter = self.run_status.RunStatusReporter("terrain-wind")
            reporter.start()
            reporter.finish("success", tiles_requested=1)
        self.assertEqual(self.store.objects, {})
        with mock.patch.dict(os.environ, {"RUN_STATUS_SKIP": ""}):
            reporter = self.run_status.RunStatusReporter("terrain-wind")
            reporter.start()
        self.assertEqual(len(self.store.objects), 2)

    def test_success(self):
        planned = ["a", "b", "c"]
        code, latest, history = self.finalize(
            planned,
            [
                {"tiles_ok": ["a", "b"], "tiles_failed": [], "uploaded_bytes": 100, "runtime_s": 400},
                {"tiles_ok": ["c"], "tiles_failed": [], "uploaded_bytes": 50, "runtime_s": 200},
            ],
        )
        self.assertEqual(code, 0)
        self.assertEqual(latest["conclusion"], "success")
        self.assertEqual(latest["tiles_requested"], 3)
        self.assertEqual(latest["tiles_ok"], ["a", "b", "c"])
        self.assertEqual(latest["tiles_failed"], [])
        self.assertEqual(latest["tiles_capped"], ["tz"])
        self.assertEqual(latest["areas_oversized"], 2)
        self.assertEqual(latest["areas_coarse"], 1)
        self.assertEqual(latest["hrrr_cycle"], "2026-10-30T06:00:00Z")
        self.assertEqual(latest["started"], "2026-10-30T06:10:00Z")
        self.assertEqual(latest["timeout_s"], int(os.environ.get("RUN_TIMEOUT_S", "3600")))
        self.assertIsNotNone(latest["finished"])
        self.assertEqual(len(history["entries"]), 1)

    def test_partial_when_one_tile_failed(self):
        code, latest, _history = self.finalize(
            ["a", "b", "c"],
            [
                {"tiles_ok": ["a"], "tiles_failed": ["b"], "uploaded_bytes": 10, "runtime_s": 5},
                {"tiles_ok": ["c"], "tiles_failed": [], "uploaded_bytes": 10, "runtime_s": 5},
            ],
        )
        self.assertEqual(code, 1)
        self.assertEqual(latest["conclusion"], "partial")
        self.assertEqual(latest["tiles_ok"], ["a", "c"])
        self.assertEqual(latest["tiles_failed"], ["b"])

    def test_missing_batch_result_counts_as_failed(self):
        code, latest, _history = self.finalize(
            ["a", "b", "c"],
            [{"tiles_ok": ["a", "b"], "tiles_failed": [], "uploaded_bytes": 10, "runtime_s": 5}],
        )
        self.assertEqual(code, 1)
        self.assertEqual(latest["conclusion"], "partial")
        self.assertEqual(latest["tiles_failed"], ["c"])

    def test_failure_when_nothing_succeeded(self):
        code, latest, _history = self.finalize(
            ["a", "b"], [{"tiles_ok": [], "tiles_failed": ["a", "b"], "uploaded_bytes": 0, "runtime_s": 1}]
        )
        self.assertEqual(code, 1)
        self.assertEqual(latest["conclusion"], "failure")

    def test_failed_plan_is_failure_even_with_stale_results(self):
        code, latest, _history = self.finalize(
            ["a"],
            [{"tiles_ok": ["a"], "tiles_failed": [], "uploaded_bytes": 1, "runtime_s": 1}],
            plan_result="failure",
            cycle="",
        )
        self.assertEqual(code, 1)
        self.assertEqual(latest["conclusion"], "failure")
        self.assertIsNone(latest["hrrr_cycle"])

    def test_finalize_replaces_plan_start_entry_in_history(self):
        with mock.patch.dict(os.environ, {"RUN_STATUS_SKIP": ""}):
            reporter = self.run_status.RunStatusReporter("terrain-wind")
            reporter.start()
        _code, latest, history = self.finalize(
            ["a"], [{"tiles_ok": ["a"], "tiles_failed": [], "uploaded_bytes": 1, "runtime_s": 1}]
        )
        self.assertEqual(len(history["entries"]), 1)
        self.assertEqual(history["entries"][0]["conclusion"], "success")

    def test_execute_with_skip_writes_result_file_only(self):
        area = fake_areas(1)[0]
        with tempfile.TemporaryDirectory() as tmp:
            areas_file = Path(tmp) / "batch.json"
            result_file = Path(tmp) / "result" / "result-0.json"
            areas_file.write_text(json.dumps({"areas": [area]}), encoding="utf-8")
            env = {
                "AREAS_FILE": str(areas_file),
                "RUN_STATUS_SKIP": "true",
                "RUN_RESULT_FILE": str(result_file),
                "TERRAIN_WIND_HRRR_CYCLE": "2026-10-30T06:00:00Z",
                "R2_ACCOUNT_ID": "x",
                "R2_ACCESS_KEY_ID": "x",
                "R2_SECRET_ACCESS_KEY": "x",
                "R2_BUCKET": "x",
                "RUNNER_TEMP": tmp,
            }
            with mock.patch.dict(os.environ, env), mock.patch.object(
                terrain_wind, "process_area", side_effect=[12345]
            ), mock.patch.object(terrain_wind, "latest_hrrr_cycle", side_effect=AssertionError):
                terrain_wind.execute()
            result = json.loads(result_file.read_text(encoding="utf-8"))
        self.assertEqual(
            {k: result[k] for k in ("tiles_ok", "tiles_failed", "uploaded_bytes")},
            {"tiles_ok": [area["key"]], "tiles_failed": [], "uploaded_bytes": 12345},
        )
        self.assertIn("runtime_s", result)
        self.assertEqual(self.store.objects, {})


class CachedHrrrFetchTests(unittest.TestCase):
    ROWS = [
        ("TMP", "2 m above ground", "1 hour fcst"),
        ("DPT", "2 m above ground", "1 hour fcst"),
        ("RH", "2 m above ground", "1 hour fcst"),
        ("UGRD", "10 m above ground", "1 hour fcst"),
        ("VGRD", "10 m above ground", "1 hour fcst"),
        ("WIND", "10 m above ground", "0-1 hour max fcst"),
        ("PRATE", "surface", "1 hour fcst"),
        ("TCDC", "entire atmosphere", "1 hour fcst"),
        ("LCDC", "low cloud layer", "1 hour fcst"),
        ("DSWRF", "surface", "1 hour fcst"),
        ("CAPE", "surface", "1 hour fcst"),
    ]
    SIZE = 1100

    def fake_http(self, url, headers=None, timeout=120):
        self.calls.append(url)
        if url.endswith(".idx"):
            lines = [
                f"{i + 1}:{i * 100}:d=2026103006:{name}:{level}:{desc}:"
                for i, (name, level, desc) in enumerate(self.ROWS)
            ]
            return "\n".join(lines).encode(), {}
        start, end = [int(v) for v in re.match(r"bytes=(\d+)-(\d+)", headers["Range"]).groups()]
        return b"GRIB" + bytes((start + n) % 251 for n in range(end - start + 1 - 4)), {}

    def setUp(self):
        self.calls = []
        for target, value in (
            ("http_bytes", self.fake_http),
            ("content_length", lambda url: self.SIZE),
        ):
            patcher = mock.patch.object(terrain_wind, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.cycle = datetime(2026, 10, 30, 6, tzinfo=timezone.utc)

    def test_cached_bytes_match_uncached_and_second_tile_skips_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            plain = tmp / "plain.grib2"
            valid_plain, size_plain = terrain_wind.fetch_hrrr_subset(self.cycle, 3, plain)
            plain_calls = len(self.calls)
            self.calls.clear()

            cache = tmp / "cache"
            first, second = tmp / "first.grib2", tmp / "second.grib2"
            valid_a, size_a = terrain_wind.fetch_hrrr_subset_cached(self.cycle, 3, first, cache)
            first_calls = len(self.calls)
            valid_b, size_b = terrain_wind.fetch_hrrr_subset_cached(self.cycle, 3, second, cache)
            self.assertEqual(len(self.calls), first_calls)
            self.assertEqual(first_calls, plain_calls)

            self.assertEqual(plain.read_bytes(), first.read_bytes())
            self.assertEqual(plain.read_bytes(), second.read_bytes())
            self.assertEqual((valid_plain, size_plain), (valid_a, size_a))
            self.assertEqual((valid_plain, size_plain), (valid_b, size_b))
            self.assertEqual(list(cache.glob("*.part")), [])

    def test_cache_is_keyed_by_cycle_and_hour(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            cache = tmp / "cache"
            terrain_wind.fetch_hrrr_subset_cached(self.cycle, 3, tmp / "a", cache)
            self.calls.clear()
            terrain_wind.fetch_hrrr_subset_cached(self.cycle, 4, tmp / "b", cache)
            self.assertTrue(self.calls)
            self.calls.clear()
            terrain_wind.fetch_hrrr_subset_cached(self.cycle.replace(hour=5), 3, tmp / "c", cache)
            self.assertTrue(self.calls)

    def test_failed_fetch_is_not_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            cache = tmp / "cache"
            with mock.patch.object(terrain_wind, "http_bytes", side_effect=OSError("boom")):
                with self.assertRaises(RuntimeError):
                    terrain_wind.fetch_hrrr_subset_cached(self.cycle, 3, tmp / "a", cache)
            self.assertEqual(list(cache.iterdir()), [])

    def test_no_cache_dir_is_plain_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "a"
            terrain_wind.fetch_hrrr_subset_cached(self.cycle, 3, dest, None)
            self.assertTrue(dest.is_file())


class WorkflowContractTests(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text(encoding="utf-8")

    def job_block(self, name):
        match = re.search(rf"^  {name}:\n(.*?)(?=^  \w[\w-]*:\n|\Z)", self.text, re.S | re.M)
        self.assertIsNotNone(match, name)
        return match.group(1)

    def test_three_jobs_with_expected_wiring(self):
        self.assertEqual(re.findall(r"^  (\w+):\n", self.text.split("\njobs:\n")[1], re.M), ["plan", "wind", "finalize"])
        wind = self.job_block("wind")
        self.assertIn("needs: plan", wind)
        self.assertIn("max-parallel: 10", wind)
        self.assertIn("fail-fast: false", wind)
        self.assertIn("timeout-minutes: 30", wind)
        self.assertIn('RUN_STATUS_SKIP: "true"', wind)
        self.assertIn("TERRAIN_WIND_HRRR_CYCLE: ${{ needs.plan.outputs.hrrr_cycle }}", wind)
        self.assertIn("key: dem-${{ steps.batch.outputs.cache_key }}", wind)
        self.assertRegex(wind, r"restore-keys: \|\n\s+dem-\n")
        self.assertIn("windninja-4.0.0-cli-mass-${{ runner.os }}-v1", wind)
        finalize = self.job_block("finalize")
        self.assertIn("needs: [plan, wind]", finalize)
        self.assertIn("if: always() && needs.plan.outputs.has_areas == 'true'", finalize)
        self.assertNotIn("RUN_STATUS_SKIP", finalize)
        self.assertNotIn("RUN_STATUS_SKIP", self.job_block("plan"))

    def test_triggers_concurrency_and_inputs_kept(self):
        self.assertIn('cron: "17 */3 * * *"', self.text)
        self.assertIn("types: [areas-changed]", self.text)
        self.assertIn("group: terrain-wind\n  cancel-in-progress: false", self.text)
        for name in ("test", "diurnal", "stability", "momentum", "tiles", "hours", "prefix"):
            self.assertRegex(self.text, rf"\n      {name}:\n        description:")
        self.assertIn('RUN_TIMEOUT_S: "1800"', self.text)

    def test_areas_artifact_is_encrypted_and_no_secret_is_echoed(self):
        plan = self.job_block("plan")
        self.assertIn("openssl enc -aes-256-cbc", plan)
        self.assertIn("-pass env:AREAS_TOKEN", plan)
        for line in self.text.splitlines():
            if re.match(r"\s*(echo|printf|set -x)\b", line):
                self.fail(f"unexpected output command: {line.strip()}")
        self.assertNotIn("matrix.areas", self.text)


if __name__ == "__main__":
    unittest.main()

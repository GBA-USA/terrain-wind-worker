import importlib.util
import json
import os
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

SPEC = importlib.util.spec_from_file_location("run_status", SCRIPTS / "run_status.py")
run_status = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(run_status)


class RunStatusTests(unittest.TestCase):
    def test_start_payload_shape(self):
        store = run_status.MemoryStatusStore()
        reporter = run_status.RunStatusReporter("terrain-wind", store=store)
        reporter.start()
        latest = store.read_json(run_status.latest_key("terrain-wind"))
        self.assertEqual(latest["v"], 1)
        self.assertEqual(latest["workflow"], "terrain-wind")
        self.assertEqual(latest["conclusion"], "running")
        self.assertIsNone(latest["finished"])
        self.assertEqual(latest["timeout_s"], 3600)

    def test_finish_payload_shape(self):
        store = run_status.MemoryStatusStore()
        reporter = run_status.RunStatusReporter("offline-packs", store=store)
        reporter.start()
        reporter.finish(
            "success",
            chunks_built=1,
            chunks_skipped=0,
            chunks_failed=0,
            uploaded_mb=0.5,
        )
        latest = store.read_json(run_status.latest_key("offline-packs"))
        self.assertEqual(latest["conclusion"], "success")
        self.assertIsNotNone(latest["finished"])
        self.assertIn("runtime_s", latest)
        self.assertEqual(latest["chunks_built"], 1)

    def test_history_keeps_forty_and_replaces_same_run(self):
        store = run_status.MemoryStatusStore()
        for index in range(45):
            reporter = run_status.RunStatusReporter("terrain-wind", store=store)
            reporter.run_id = f"run-{index}"
            reporter.run_attempt = 1
            reporter.start()
            reporter.finish("success", tiles_requested=0, tiles_ok=[], tiles_failed=[], tiles_capped=[])
        history = store.read_json(run_status.history_key("terrain-wind"))
        self.assertEqual(len(history["entries"]), 40)
        self.assertEqual(history["entries"][0]["run_id"], "run-44")

        reporter = run_status.RunStatusReporter("terrain-wind", store=store)
        reporter.run_id = "run-44"
        reporter.run_attempt = 1
        reporter.start()
        reporter.finish("failure", tiles_requested=1, tiles_ok=[], tiles_failed=["t1_1"], tiles_capped=[])
        history = store.read_json(run_status.history_key("terrain-wind"))
        self.assertEqual(len(history["entries"]), 40)
        self.assertEqual(history["entries"][0]["conclusion"], "failure")

    def test_test_mode_uses_status_test_prefix(self):
        prior = os.environ.get("TEST_MODE")
        try:
            os.environ["TEST_MODE"] = "true"
            self.assertEqual(run_status.status_root_dir("terrain-wind"), "status-test")
            self.assertTrue(run_status.latest_key("terrain-wind").startswith("status-test/"))
        finally:
            if prior is None:
                os.environ.pop("TEST_MODE", None)
            else:
                os.environ["TEST_MODE"] = prior

    def test_write_failure_does_not_raise(self):
        class BrokenStore(run_status.MemoryStatusStore):
            def write_json(self, key, payload, cache_control="no-cache"):
                raise OSError("disk full")

        reporter = run_status.RunStatusReporter("terrain-wind", store=BrokenStore())
        reporter.start()
        reporter.finish("success", tiles_requested=0, tiles_ok=[], tiles_failed=[], tiles_capped=[])

    def test_payload_has_no_sensitive_fields(self):
        store = run_status.MemoryStatusStore()
        reporter = run_status.RunStatusReporter("terrain-wind", store=store)
        reporter.start()
        reporter.finish(
            "success",
            tiles_requested=1,
            tiles_ok=["t322_-816"],
            tiles_failed=[],
            tiles_capped=[],
            areas_oversized=0,
            areas_coarse=0,
            hrrr_cycle="2026-10-28T12:00:00Z",
        )
        latest = store.read_json(run_status.latest_key("terrain-wind"))
        encoded = json.dumps(latest)
        self.assertNotIn("bbox", encoded)
        self.assertNotIn("http", encoded.lower())


if __name__ == "__main__":
    unittest.main()

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("terrain_wind", ROOT / "scripts" / "terrain_wind.py")
terrain_wind = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(terrain_wind)


class HourRecordTests(unittest.TestCase):
    def test_payload_separates_version_from_north_wind(self):
        bbox = [-79.29, 38.43, -79.19, 38.49]
        payload = terrain_wind.hour_record_payload(
            "test-reddish-knob",
            "2026-10-25T12:00:00Z",
            bbox,
            2,
            2,
            100.0,
            [1.2, None],
            [0.0, 3.4],
            run_stamp="2026-10-25T11:00:00Z",
        )
        self.assertEqual(payload["ver"], 1)
        self.assertIsInstance(payload["v"], list)
        self.assertEqual(payload["v"], [0.0, 3.4])
        encoded = json.dumps(payload, separators=(",", ":"))
        parsed = json.loads(encoded)
        self.assertEqual(parsed["ver"], 1)
        self.assertEqual(parsed["v"], [0.0, 3.4])


class SanitizeGridTests(unittest.TestCase):
    def test_nodata_becomes_null(self):
        values = [-9999.0, float("nan"), 2.56]
        cleaned = terrain_wind.sanitize_resampled_values(values, -9999.0)
        self.assertIsNone(cleaned[0])
        self.assertIsNone(cleaned[1])
        self.assertEqual(cleaned[2], 2.6)

    def test_calm_air_stays_zero(self):
        cleaned = terrain_wind.sanitize_resampled_values([0.04, -0.02], -9999.0)
        self.assertEqual(cleaned, [0.0, 0.0])


class TestFixtureTests(unittest.TestCase):
    def test_fixture_lists_both_test_areas(self):
        document = json.loads((ROOT / "fixtures" / "test-area.json").read_text(encoding="utf-8"))
        keys = {area["key"] for area in document["areas"]}
        self.assertEqual(
            keys,
            {"fixture-park-ridge", "test-reddish-knob"},
        )
        reddish = next(a for a in document["areas"] if a["key"] == "test-reddish-knob")
        self.assertEqual(reddish["bbox"], [-79.29, 38.43, -79.19, 38.49])

    def test_resolve_test_mode_writes_both_areas(self):
        with tempfile.TemporaryDirectory() as tmp:
            areas_file = Path(tmp) / "areas.json"
            env = {
                **os.environ,
                "TEST_MODE": "true",
                "AREAS_FILE": str(areas_file),
                "GITHUB_OUTPUT": str(Path(tmp) / "github_output"),
            }
            prior = {key: os.environ.get(key) for key in ("TEST_MODE", "AREAS_FILE", "GITHUB_OUTPUT")}
            try:
                os.environ.update(env)
                terrain_wind.resolve()
            finally:
                for key, value in prior.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
            resolved = json.loads(areas_file.read_text(encoding="utf-8"))
            keys = {area["key"] for area in resolved["areas"]}
            self.assertEqual(keys, {"fixture-park-ridge", "test-reddish-knob"})


if __name__ == "__main__":
    unittest.main()

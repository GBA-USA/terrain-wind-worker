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


class WindNinjaCfgTests(unittest.TestCase):
    def options(self, **overrides):
        base = {
            "diurnal": True,
            "stability": True,
            "momentum": False,
            "utc_hours": None,
            "output_prefix": "wind",
        }
        base.update(overrides)
        return base

    def pairs(self, **overrides):
        return dict(terrain_wind.build_base_cfg_pairs("/dem.tif", "/out", self.options(**overrides)))

    def test_defaults_match_production_solver(self):
        pairs = self.pairs()
        self.assertEqual(pairs["diurnal_winds"], "true")
        self.assertEqual(pairs["non_neutral_stability"], "true")
        self.assertNotIn("momentum_flag", pairs)

    def test_diurnal_off(self):
        pairs = self.pairs(diurnal=False)
        self.assertEqual(pairs["diurnal_winds"], "false")
        self.assertEqual(pairs["non_neutral_stability"], "true")

    def test_stability_off(self):
        pairs = self.pairs(stability=False)
        self.assertEqual(pairs["non_neutral_stability"], "false")

    def test_momentum_adds_iterations_and_disables_stability(self):
        pairs = self.pairs(momentum=True, stability=True)
        self.assertEqual(pairs["momentum_flag"], "true")
        self.assertEqual(pairs["number_of_iterations"], str(terrain_wind.MOMENTUM_ITERATIONS))
        self.assertEqual(pairs["non_neutral_stability"], "false")

    def test_momentum_off_with_stability_off(self):
        pairs = self.pairs(momentum=False, stability=False)
        self.assertNotIn("momentum_flag", pairs)
        self.assertEqual(pairs["non_neutral_stability"], "false")

    def test_run_options_from_env_defaults(self):
        keys = (
            "TERRAIN_WIND_DIURNAL",
            "TERRAIN_WIND_STABILITY",
            "TERRAIN_WIND_MOMENTUM",
            "TERRAIN_WIND_HOURS",
            "TERRAIN_WIND_PREFIX",
        )
        prior = {key: os.environ.pop(key, None) for key in keys}
        try:
            options = terrain_wind.windninja_run_options_from_env()
        finally:
            for key, value in prior.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.assertTrue(options["diurnal"])
        self.assertTrue(options["stability"])
        self.assertFalse(options["momentum"])
        self.assertIsNone(options["utc_hours"])
        self.assertEqual(options["output_prefix"], "wind")

    def test_run_options_from_env_overrides(self):
        prior = {
            key: os.environ.get(key)
            for key in (
                "TERRAIN_WIND_DIURNAL",
                "TERRAIN_WIND_STABILITY",
                "TERRAIN_WIND_MOMENTUM",
                "TERRAIN_WIND_HOURS",
                "TERRAIN_WIND_PREFIX",
            )
        }
        try:
            os.environ["TERRAIN_WIND_DIURNAL"] = "false"
            os.environ["TERRAIN_WIND_STABILITY"] = "false"
            os.environ["TERRAIN_WIND_MOMENTUM"] = "true"
            os.environ["TERRAIN_WIND_HOURS"] = "16,17"
            os.environ["TERRAIN_WIND_PREFIX"] = "solver-test"
            options = terrain_wind.windninja_run_options_from_env()
        finally:
            for key, value in prior.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.assertFalse(options["diurnal"])
        self.assertFalse(options["stability"])
        self.assertTrue(options["momentum"])
        self.assertEqual(options["utc_hours"], {16, 17})
        self.assertEqual(options["output_prefix"], "solver-test")


class OutputPrefixTests(unittest.TestCase):
    def test_wind_prefix_unchanged(self):
        self.assertEqual(terrain_wind.normalize_output_prefix("wind"), "wind")
        self.assertEqual(terrain_wind.object_prefix_for_area("t322_-816", "wind"), "wind/t322_-816/")

    def test_alternate_prefix(self):
        self.assertEqual(terrain_wind.normalize_output_prefix("solver-test"), "solver-test")
        self.assertEqual(
            terrain_wind.object_prefix_for_area("t386_-832", "solver-test"),
            "solver-test/t386_-832/",
        )

    def test_invalid_prefix_rejected(self):
        with self.assertRaises(RuntimeError):
            terrain_wind.normalize_output_prefix("../wind")


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

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


class CellMValidationTests(unittest.TestCase):
    def test_invalid_cell_m_defaults_to_100(self):
        document = {
            "areas": [
                {"key": "a", "bbox": [-81.6, 32.2, -81.5, 32.3], "cell_m": 150},
            ]
        }
        areas = terrain_wind.validate_areas(document)
        self.assertEqual(areas[0]["cell_m"], 100)

    def test_edge_limits_40_and_80_km(self):
        # ~35 km square fits at 100 m
        small = {"areas": [{"key": "ok", "bbox": [-81.0, 32.0, -80.68, 32.32], "cell_m": 100}]}
        stats = {}
        self.assertEqual(len(terrain_wind.validate_areas(small, stats)), 1)
        self.assertEqual(stats["areas_oversized"], 0)

        too_big_100 = {"areas": [{"key": "big", "bbox": [-82.0, 32.0, -81.5, 32.5], "cell_m": 100}]}
        stats = {}
        self.assertEqual(len(terrain_wind.validate_areas(too_big_100, stats)), 0)
        self.assertEqual(stats["areas_oversized"], 1)

        coarse_ok = {"areas": [{"key": "wide", "bbox": [-82.0, 32.0, -81.2, 32.5], "cell_m": 200}]}
        stats = {}
        self.assertEqual(len(terrain_wind.validate_areas(coarse_ok, stats)), 1)
        self.assertEqual(stats["areas_coarse"], 1)

    def test_overlap_smallest_cell_wins(self):
        areas = [
            {"key": "fine", "bbox": [-81.6, 32.2, -81.4, 32.4], "cell_m": 100},
            {"key": "coarse", "bbox": [-81.55, 32.25, -81.45, 32.35], "cell_m": 200},
        ]
        expanded = terrain_wind.expand_areas_to_tiles(areas)
        by_key = {item["key"]: item["cell_m"] for item in expanded}
        for cell_m in by_key.values():
            self.assertEqual(cell_m, 100)

    def test_budget_prefers_100m_and_reports_capped(self):
        areas = []
        for lng_i in range(80):
            areas.append(
                {
                    "key": f"a-{lng_i}",
                    "bbox": terrain_wind.tile_bbox(322, lng_i),
                    "cell_m": 100,
                }
            )
        for lng_i in range(80, 120):
            areas.append(
                {
                    "key": f"b-{lng_i}",
                    "bbox": terrain_wind.tile_bbox(323, lng_i),
                    "cell_m": 200,
                }
            )
        capped = []
        selected = terrain_wind.expand_areas_to_tiles(areas, capped_out=capped)
        self.assertTrue(all(item["cell_m"] == 100 for item in selected))
        self.assertGreater(len(capped), 0)
        cost = sum(terrain_wind.tile_budget_cost(item["cell_m"]) for item in selected)
        self.assertLessEqual(cost, terrain_wind.TILE_BUDGET + 1e-6)

    def test_grid_shape_200m_halves_nx_ny(self):
        bbox = [-81.5901, 32.2670, -81.5632, 32.2927]
        nx100, ny100, _ = terrain_wind.grid_shape(bbox, cell_m=100)
        nx200, ny200, _ = terrain_wind.grid_shape(bbox, cell_m=200)
        self.assertAlmostEqual(nx200, nx100 / 2, delta=1)
        self.assertAlmostEqual(ny200, ny100 / 2, delta=1)

    def test_default_100m_cfg_unchanged(self):
        options = {
            "diurnal": True,
            "stability": True,
            "momentum": False,
            "utc_hours": None,
            "output_prefix": "wind",
        }
        pairs = dict(terrain_wind.build_base_cfg_pairs("/dem.tif", "/out", options, cell_m=100))
        self.assertEqual(pairs["mesh_resolution"], "100")
        self.assertEqual(pairs["ascii_out_resolution"], "100")


if __name__ == "__main__":
    unittest.main()

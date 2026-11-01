import importlib.util
import os
import stat
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("terrain_wind", ROOT / "scripts" / "terrain_wind.py")
terrain_wind = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(terrain_wind)

AREA = {"key": "tile-a", "bbox": [-79.3, 38.4, -79.2, 38.5]}
OPTIONS = {
    "diurnal": True,
    "stability": True,
    "momentum": False,
    "vegetation_from_cover": False,
    "utc_hours": None,
    "output_prefix": "wind",
}
MAIN_PAIRS = [
    ("num_threads", "2"),
    ("elevation_file", "/dem.tif"),
    ("time_zone", "UTC"),
    ("output_wind_height", "10"),
    ("units_output_wind_height", "m"),
    ("output_speed_units", "mps"),
    ("vegetation", "grass"),
    ("mesh_resolution", "100"),
    ("units_mesh_resolution", "m"),
    ("diurnal_winds", "true"),
    ("non_neutral_stability", "true"),
    ("write_ascii_output", "true"),
    ("ascii_out_aaigrid", "true"),
    ("ascii_out_json", "false"),
    ("ascii_out_proj", "false"),
    ("ascii_out_geog", "true"),
    ("ascii_out_uv", "true"),
    ("ascii_out_resolution", "100"),
    ("units_ascii_out_resolution", "m"),
    ("write_goog_output", "false"),
    ("write_shapefile_output", "false"),
    ("write_farsite_atm", "false"),
    ("write_pdf_output", "false"),
    ("write_vtk_output", "false"),
    ("write_geotiff_output", "false"),
    ("output_path", "/out"),
]


class VegetationForCountsTests(unittest.TestCase):
    def test_all_forest_is_trees(self):
        self.assertEqual(terrain_wind.vegetation_for_counts({41: 40, 42: 30, 43: 20, 90: 10}), "trees")

    def test_half_forest_is_trees(self):
        self.assertEqual(terrain_wind.vegetation_for_counts({41: 50, 81: 50}), "trees")

    def test_forest_plus_shrub_is_brush(self):
        self.assertEqual(terrain_wind.vegetation_for_counts({41: 40, 52: 20, 81: 40}), "brush")

    def test_pasture_and_crops_are_grass(self):
        self.assertEqual(terrain_wind.vegetation_for_counts({81: 60, 82: 30, 41: 10}), "grass")

    def test_water_only_is_grass(self):
        self.assertEqual(terrain_wind.vegetation_for_counts({11: 10000}), "grass")

    def test_under_min_pixels_is_grass(self):
        self.assertEqual(terrain_wind.vegetation_for_counts({41: 49}), "grass")

    def test_exactly_min_pixels_counts(self):
        self.assertEqual(terrain_wind.vegetation_for_counts({41: 50}), "trees")

    def test_unclassified_water_and_nodata_do_not_dilute_shares(self):
        self.assertEqual(terrain_wind.vegetation_for_counts({0: 500, 11: 500, 250: 500, 41: 60, 81: 40}), "trees")


class CoverVegetationForTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.cache = Path(temp.name) / "dem-cache"

    def test_forest_grid_gives_trees(self):
        with mock.patch.object(terrain_wind, "fetch_cover_counts", return_value={41: 500}):
            self.assertEqual(terrain_wind.cover_vegetation_for(AREA, self.cache), "trees")

    def test_fetch_failure_gives_grass_and_is_not_cached(self):
        with mock.patch.object(terrain_wind, "fetch_cover_counts", side_effect=RuntimeError("down")):
            self.assertEqual(terrain_wind.cover_vegetation_for(AREA, self.cache), "grass")
        with mock.patch.object(terrain_wind, "fetch_cover_counts", return_value={41: 500}) as fetch:
            self.assertEqual(terrain_wind.cover_vegetation_for(AREA, self.cache), "trees")
        fetch.assert_called_once()

    def test_outside_conus_gives_grass_without_fetching(self):
        area = {"key": "tile-ak", "bbox": [-149.9, 61.2, -149.8, 61.3]}
        with mock.patch.object(terrain_wind, "fetch_cover_counts", side_effect=AssertionError("fetched")) as fetch:
            self.assertEqual(terrain_wind.cover_vegetation_for(area, self.cache), "grass")
        fetch.assert_not_called()

    def test_same_bbox_hits_cache(self):
        with mock.patch.object(terrain_wind, "fetch_cover_counts", return_value={52: 500}) as fetch:
            self.assertEqual(terrain_wind.cover_vegetation_for(AREA, self.cache), "brush")
            self.assertEqual(terrain_wind.cover_vegetation_for(AREA, self.cache), "brush")
        fetch.assert_called_once()

    def test_changed_bbox_misses_cache(self):
        moved = {"key": AREA["key"], "bbox": [-79.2, 38.4, -79.1, 38.5]}
        with mock.patch.object(terrain_wind, "fetch_cover_counts", side_effect=[{41: 500}, {81: 500}]) as fetch:
            self.assertEqual(terrain_wind.cover_vegetation_for(AREA, self.cache), "trees")
            self.assertEqual(terrain_wind.cover_vegetation_for(moved, self.cache), "grass")
        self.assertEqual(fetch.call_count, 2)

    def test_dem_refresh_does_not_satisfy_the_vegetation_stamp(self):
        moved = {"key": AREA["key"], "bbox": [-79.2, 38.4, -79.1, 38.5]}

        def fake_dem(_bbox, dest):
            dest.write_bytes(b"dem")

        with mock.patch.object(terrain_wind, "download_dem", side_effect=fake_dem):
            with mock.patch.object(terrain_wind, "fetch_cover_counts", side_effect=[{41: 500}, {81: 500}]):
                terrain_wind.dem_for(AREA, self.cache)
                self.assertEqual(terrain_wind.cover_vegetation_for(AREA, self.cache), "trees")
                terrain_wind.dem_for(moved, self.cache)
                self.assertEqual(terrain_wind.cover_vegetation_for(moved, self.cache), "grass")


class FetchCoverCountsTests(unittest.TestCase):
    def fake_gdal(self, directory):
        path = Path(directory) / "gdal_translate"
        path.write_text(
            "#!/bin/sh\n"
            'for last; do :; done\n'
            'printf "ncols 3\\nnrows 2\\nxllcorner 0\\nyllcorner 0\\n'
            'dx 0.0004\\ndy 0.0003\\nNODATA_value 0\\n0 41 41\\n52 11 41\\n" > "$last"\n',
            encoding="utf-8",
        )
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return str(path)

    def test_counts_classes_and_requests_the_tile(self):
        calls = []

        def fake_retry(_label, url, **_kwargs):
            calls.append(url)
            return b"II*\x00", {}

        real_run = terrain_wind.run

        with tempfile.TemporaryDirectory() as tmp:
            gdal = self.fake_gdal(tmp)

            def run_fake(cmd, **kwargs):
                calls.append(cmd)
                return real_run([gdal, *cmd[1:]], **kwargs)

            with mock.patch.object(terrain_wind, "http_bytes_retry", side_effect=fake_retry):
                with mock.patch.object(terrain_wind, "run", side_effect=run_fake):
                    counts = terrain_wind.fetch_cover_counts(AREA["bbox"])

        self.assertEqual(counts, {41: 3, 52: 1, 11: 1})
        query = parse_qs(urlparse(calls[0]).query)
        self.assertEqual(query["coverageId"], ["mrlc_download__NLCD_2021_Land_Cover_L48"])
        self.assertEqual(query["format"], ["image/tiff"])
        self.assertEqual(query["subset"], ["Long(-79.3,-79.2)", "Lat(38.4,38.5)"])
        projwin = calls[1].index("-projwin")
        self.assertEqual(calls[1][projwin + 1 : projwin + 5], ["-79.3", "38.5", "-79.2", "38.4"])


class SettingTests(unittest.TestCase):
    def test_default_pairs_equal_main(self):
        self.assertEqual(terrain_wind.build_base_cfg_pairs("/dem.tif", "/out", OPTIONS), MAIN_PAIRS)

    def test_only_the_vegetation_pair_changes(self):
        pairs = terrain_wind.build_base_cfg_pairs("/dem.tif", "/out", OPTIONS, vegetation="trees")
        self.assertEqual(
            [(a, b) for a, b in zip(MAIN_PAIRS, pairs) if a != b],
            [(("vegetation", "grass"), ("vegetation", "trees"))],
        )

    def test_setting_is_off_when_unset(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("TERRAIN_WIND_VEGETATION_FROM_COVER", None)
            self.assertFalse(terrain_wind.windninja_run_options_from_env()["vegetation_from_cover"])

    def test_setting_reads_true(self):
        with mock.patch.dict(os.environ, {"TERRAIN_WIND_VEGETATION_FROM_COVER": "true"}):
            self.assertTrue(terrain_wind.windninja_run_options_from_env()["vegetation_from_cover"])


class ProcessAreaTests(unittest.TestCase):
    def run_area(self, options, **cover):
        cfgs = []
        cycle = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)

        def fake_windninja(cfg, _env):
            cfgs.append(Path(cfg).read_text(encoding="utf-8"))
            raise RuntimeError("stop after cfg")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "dem.tif").write_bytes(b"dem")
            with (
                mock.patch.dict(os.environ, {"RUNNER_TEMP": tmp}),
                mock.patch.object(terrain_wind, "dem_for", return_value=root / "dem.tif"),
                mock.patch.object(terrain_wind, "windninja_env", return_value={}),
                mock.patch.object(terrain_wind, "fetch_hrrr_subset_cached", return_value=(cycle, 1)),
                mock.patch.object(terrain_wind, "weather_from_grib", return_value={}),
                mock.patch.object(terrain_wind, "run_windninja", side_effect=fake_windninja),
                mock.patch.object(terrain_wind, "build_speed_dir", return_value=("s", "d", 1.0)),
                mock.patch.object(terrain_wind, "surface_means", return_value=(10.0, 20.0)),
                mock.patch.object(terrain_wind, "FORECAST_HOURS", range(1, 2)),
                mock.patch.object(terrain_wind, "fetch_cover_counts", **cover) as fetch,
            ):
                with self.assertRaisesRegex(RuntimeError, "no hours produced"):
                    terrain_wind.process_area(AREA, cycle, Path(tmp), root / "cache", (), options)
        return cfgs, fetch

    def test_setting_off_never_fetches(self):
        cfgs, fetch = self.run_area(OPTIONS, side_effect=AssertionError("fetched"))
        fetch.assert_not_called()
        self.assertTrue(cfgs)
        self.assertTrue(all("vegetation = grass" in cfg for cfg in cfgs))

    def test_setting_on_solves_with_forest_vegetation(self):
        cfgs, fetch = self.run_area({**OPTIONS, "vegetation_from_cover": True}, return_value={41: 500})
        fetch.assert_called_once()
        self.assertEqual(len(cfgs), 2)
        self.assertTrue(all("vegetation = trees" in cfg for cfg in cfgs))

    def test_failed_fetch_keeps_the_area_on_grass(self):
        cfgs, _fetch = self.run_area({**OPTIONS, "vegetation_from_cover": True}, side_effect=RuntimeError("down"))
        self.assertEqual(len(cfgs), 2)
        self.assertTrue(all("vegetation = grass" in cfg for cfg in cfgs))


if __name__ == "__main__":
    unittest.main()

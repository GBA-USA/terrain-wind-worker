import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

SPEC = importlib.util.spec_from_file_location("contour_tiles", SCRIPTS / "contour_tiles.py")
contour_tiles = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(contour_tiles)


class BboxMathTests(unittest.TestCase):
    def test_512_grid_matches_app_256_bbox_union(self):
        cases = [(11, 512, 768), (12, 1024, 1536), (15, 8192, 12288)]
        for zoom, x, y in cases:
            parent = contour_tiles.tile_bbox_epsg3857(zoom, x, y)
            union = contour_tiles.app_256_bbox_union_epsg3857(zoom, x, y)
            for idx in range(4):
                self.assertAlmostEqual(parent[idx], union[idx], places=3)

    def test_export_bbox_uses_parent_tile_extent(self):
        zoom, x, y = 12, 1024, 1536
        params = contour_tiles.contour_export_params(zoom, x, y, size=512)
        xmin, ymin, xmax, ymax = (float(part) for part in params["bbox"].split(","))
        expected = contour_tiles.tile_bbox_epsg3857(zoom, x, y)
        self.assertEqual((xmin, ymin, xmax, ymax), expected)
        self.assertEqual(params["size"], "512,512")
        self.assertEqual(params["bboxSR"], "3857")


class KeyFormatTests(unittest.TestCase):
    def test_object_key_layout(self):
        self.assertEqual(
            contour_tiles.object_key(11, 512, 768),
            "tiles/contours/v1/512/11/512/768.png",
        )


class SkipLogicTests(unittest.TestCase):
    def test_fresh_object_skipped(self):
        now = datetime(2026, 10, 27, tzinfo=timezone.utc)
        meta = {"LastModified": "2026-09-01T12:00:00Z"}
        with mock.patch.object(contour_tiles, "head_object", return_value=meta):
            self.assertTrue(
                contour_tiles.object_is_fresh("tiles/contours/v1/512/11/0/0.png", {}, "e", "b", now=now)
            )

    def test_old_object_rebuilt(self):
        now = datetime(2026, 10, 27, tzinfo=timezone.utc)
        meta = {"LastModified": "2026-05-01T12:00:00Z"}
        with mock.patch.object(contour_tiles, "head_object", return_value=meta):
            self.assertFalse(
                contour_tiles.object_is_fresh("tiles/contours/v1/512/11/0/0.png", {}, "e", "b", now=now)
            )

    def test_missing_object_rebuilt(self):
        with mock.patch.object(contour_tiles, "head_object", return_value=None):
            self.assertFalse(contour_tiles.object_is_fresh("k", {}, "e", "b"))


class CollectTilesTests(unittest.TestCase):
    def test_fixture_area_tile_count_within_cap(self):
        fixture = json.loads((ROOT / "fixtures" / "test-area.json").read_text(encoding="utf-8"))
        areas = contour_tiles.validate_areas(fixture)
        tiles = contour_tiles.collect_tiles(areas, [])
        self.assertGreater(len(tiles), 0)
        self.assertLessEqual(len(tiles), contour_tiles.MAX_TILES_PER_RUN)

    def test_miss_tiles_merged(self):
        areas = [{"key": "a", "bbox": [-105.69, 40.39, -105.68, 40.40]}]
        extra = [(15, 1, 2)]
        tiles = contour_tiles.collect_tiles(areas, extra)
        self.assertIn((15, 1, 2), tiles)


class LayersTests(unittest.TestCase):
    def test_layer_sets_by_zoom(self):
        self.assertEqual(contour_tiles.contour_layers_for_z(11), "show:10,11,12,13")
        self.assertEqual(contour_tiles.contour_layers_for_z(12), "show:15,16,17,18")


class MissParseTests(unittest.TestCase):
    def test_parse_miss_list(self):
        tiles = contour_tiles.parse_miss_tiles({"tiles": [[11, 1, 2], [12, 3, 4]]})
        self.assertEqual(tiles, [(11, 1, 2), (12, 3, 4)])


class PlanCommandTests(unittest.TestCase):
    def test_plan_logs_tile_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            areas_file = Path(tmp) / "areas.json"
            areas_file.write_text((ROOT / "fixtures" / "test-area.json").read_text(encoding="utf-8"))
            with mock.patch.dict(os.environ, {"AREAS_FILE": str(areas_file), "MISSES_URL": ""}), mock.patch.object(
                contour_tiles, "log"
            ) as log_mock:
                contour_tiles.plan()
        messages = " ".join(str(call.args[0]) for call in log_mock.call_args_list)
        self.assertIn("tiles_planned=", messages)
        self.assertIn("no misses source", messages)


if __name__ == "__main__":
    unittest.main()

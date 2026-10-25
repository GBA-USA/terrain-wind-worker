import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("terrain_wind", ROOT / "scripts" / "terrain_wind.py")
terrain_wind = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(terrain_wind)


class TileMathTests(unittest.TestCase):
    def test_point_key_negative_longitude(self):
        lat_i, lng_i = terrain_wind.tile_indices_for_point(32.28, -81.58)
        self.assertEqual((lat_i, lng_i), (322, -816))
        self.assertEqual(terrain_wind.tile_key(lat_i, lng_i), "t322_-816")

    def test_point_on_tile_edge_uses_south_west_tile(self):
        lat_i, lng_i = terrain_wind.tile_indices_for_point(32.2, -81.6)
        self.assertEqual(terrain_wind.tile_key(lat_i, lng_i), "t322_-816")
        bbox = terrain_wind.tile_bbox(lat_i, lng_i)
        self.assertEqual(bbox, [-81.6, 32.2, -81.5, 32.3])

    def test_sample_bbox_single_tile(self):
        bbox = [-81.5901, 32.2670, -81.5632, 32.2927]
        tiles = terrain_wind.tiles_for_bbox(bbox)
        self.assertEqual(set(tiles.keys()), {"t322_-816"})
        self.assertEqual(tiles["t322_-816"], [-81.6, 32.2, -81.5, 32.3])

    def test_overlapping_areas_dedupe(self):
        a = terrain_wind.tiles_for_bbox([-81.5901, 32.2670, -81.5632, 32.2927])
        b = terrain_wind.tiles_for_bbox([-81.6240, 32.2741, -81.5878, 32.2971])
        merged = {}
        merged.update(a)
        merged.update(b)
        expanded = terrain_wind.areas_from_tiles(merged)
        keys = [area["key"] for area in expanded]
        self.assertEqual(keys, ["t322_-816", "t322_-817"])

    def test_tile_cap_keeps_first_sixty_sorted(self):
        areas = [
            {
                "key": f"area-{lng_i}",
                "bbox": terrain_wind.tile_bbox(322, lng_i),
            }
            for lng_i in range(100)
        ]
        expected = sorted(terrain_wind.tile_key(322, lng_i) for lng_i in range(100))[: terrain_wind.TILE_CAP]
        capped = terrain_wind.expand_areas_to_tiles(areas)
        self.assertEqual(len(capped), terrain_wind.TILE_CAP)
        keys = [area["key"] for area in capped]
        self.assertEqual(keys, expected)


if __name__ == "__main__":
    unittest.main()

import gzip
import hashlib
import importlib.util
import io
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

SPEC = importlib.util.spec_from_file_location("offline_packs", SCRIPTS / "offline_packs.py")
offline_packs = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(offline_packs)

TW_SPEC = importlib.util.spec_from_file_location("terrain_wind", SCRIPTS / "terrain_wind.py")
terrain_wind = importlib.util.module_from_spec(TW_SPEC)
assert TW_SPEC.loader is not None
TW_SPEC.loader.exec_module(terrain_wind)


class ChunkTileTests(unittest.TestCase):
    def test_iverson_bbox_plus_margin_z14(self):
        bbox = [-81.5901, 32.2670, -81.5632, 32.2927]
        padded = terrain_wind.buffer_bbox(bbox, 400)
        tiles = offline_packs.z14_tiles_for_bbox(padded)
        self.assertEqual(
            sorted(tiles),
            [
                (4478, 6637),
                (4478, 6638),
                (4478, 6639),
                (4479, 6637),
                (4479, 6638),
                (4479, 6639),
                (4480, 6637),
                (4480, 6638),
                (4480, 6639),
            ],
        )

    def test_negative_longitude_tile_indices(self):
        x, y = offline_packs.lonlat_to_tile(-81.58, 32.28, 14)
        self.assertIsInstance(x, int)
        self.assertIsInstance(y, int)
        self.assertGreaterEqual(x, 0)
        self.assertGreaterEqual(y, 0)

    def test_dedupe_across_two_areas(self):
        areas = [
            {"key": "a", "bbox": [-81.5901, 32.2670, -81.5632, 32.2927]},
            {"key": "b", "bbox": [-81.6240, 32.2741, -81.5878, 32.2971]},
        ]
        chunks = offline_packs.collect_chunks(areas)
        self.assertEqual(len(chunks), len(set(chunks)))
        self.assertGreater(len(chunks), 9)

    def test_per_area_cap_skips_extra_tiles(self):
        # 40 km edge → many z14 tiles; only 64 per area are kept.
        areas = [{"key": "big", "bbox": [-81.8, 32.0, -81.4, 32.36]}]
        with mock.patch.object(offline_packs, "log") as log_mock:
            chunks = offline_packs.collect_chunks(areas)
        self.assertLessEqual(len(chunks), offline_packs.MAX_CHUNKS_PER_AREA)
        messages = " ".join(str(call.args[0]) for call in log_mock.call_args_list)
        self.assertIn("chunk cap skipped", messages)

    def test_run_cap_at_four_hundred(self):
        areas = [
            {"key": f"a{i}", "bbox": [-105.69 - i * 0.35, 40.39, -105.66 - i * 0.35, 40.412]}
            for i in range(80)
        ]
        chunks = offline_packs.collect_chunks(areas)
        self.assertEqual(len(chunks), offline_packs.MAX_CHUNKS_PER_RUN)


class DemTileListTests(unittest.TestCase):
    def test_single_chunk_tile_count(self):
        tiles = offline_packs.dem_tiles_for_chunk(4479, 6638)
        self.assertEqual(len(tiles), 87)


class ManifestSkipTests(unittest.TestCase):
    def test_fresh_v1_skipped(self):
        now = datetime(2026, 10, 26, tzinfo=timezone.utc)
        manifest = {"v": 1, "built": "2026-10-20T08:00:00Z"}
        self.assertTrue(offline_packs.manifest_is_fresh(manifest, now=now))

    def test_old_manifest_rebuilt(self):
        now = datetime(2026, 10, 26, tzinfo=timezone.utc)
        manifest = {"v": 1, "built": "2026-08-01T08:00:00Z"}
        self.assertFalse(offline_packs.manifest_is_fresh(manifest, now=now))

    def test_missing_or_v0_rebuilt(self):
        now = datetime(2026, 10, 26, tzinfo=timezone.utc)
        self.assertFalse(offline_packs.manifest_is_fresh(None, now=now))
        self.assertFalse(offline_packs.manifest_is_fresh({"v": 0, "built": "2026-10-20T08:00:00Z"}, now=now))


class PadusClipTests(unittest.TestCase):
    def test_polygon_crossing_bbox_produces_fill_and_edge(self):
        bbox = offline_packs.tile_bbox_wgs84(14, 4479, 6638)
        west, south, east, north = bbox
        mid_w = (west + east) / 2
        geojson = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {"OBJECTID": 42, "Category": "Fee"},
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [
                                [west - 0.01, south],
                                [east + 0.01, south],
                                [east + 0.01, north],
                                [west - 0.01, north],
                                [west - 0.01, south],
                            ]
                        ],
                    },
                }
            ],
        }
        features = offline_packs.public_land_features(geojson, bbox)
        roles = {feature["properties"]["role"] for feature in features}
        self.assertIn("fill", roles)
        self.assertIn("edge", roles)
        self.assertEqual(features[0]["properties"]["OBJECTID"], 42)

    def test_empty_collection_valid(self):
        features = offline_packs.public_land_features({"type": "FeatureCollection", "features": []}, [0, 0, 1, 1])
        self.assertEqual(features, [])


class ManifestSchemaTests(unittest.TestCase):
    def test_manifest_bytes_and_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            dem = Path(tmp) / "dem.pmtiles"
            land = Path(tmp) / "public-land.geojson"
            dem.write_bytes(b"dem-bytes")
            land.write_bytes(b'{"type":"FeatureCollection","features":[]}')
            bbox = [1.0, 2.0, 3.0, 4.0]
            manifest = offline_packs.manifest_for_chunk(1, 2, bbox, "2026-10-26T08:00:00Z", dem, land, 0)
            self.assertEqual(manifest["v"], 1)
            self.assertEqual(manifest["chunk"], [14, 1, 2])
            self.assertEqual(manifest["bbox"], bbox)
            self.assertEqual(manifest["bytes"], dem.stat().st_size + land.stat().st_size)
            dem_entry = manifest["files"][0]
            land_entry = manifest["files"][1]
            self.assertEqual(dem_entry["sha256"], hashlib.sha256(b"dem-bytes").hexdigest())
            self.assertEqual(land_entry["sha256"], hashlib.sha256(land.read_bytes()).hexdigest())
            self.assertEqual(dem_entry["zooms"], [12, 17])
            self.assertEqual(dem_entry["tileUrl"], offline_packs.MAP_TILE_URL)

    def test_upload_order_manifest_last(self):
        calls = []

        def fake_upload(local, key, env, endpoint, bucket, gzip_encoding, content_type="application/json", cache_control=None):
            calls.append(key.split("/")[-1])
            return Path(local).stat().st_size

        def write_dem(path, x, y):
            path.write_bytes(b"dem")

        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            with mock.patch.object(offline_packs, "write_dem_pmtiles", side_effect=write_dem), mock.patch.object(
                offline_packs, "padus_query_geojson", return_value={"type": "FeatureCollection", "features": []}
            ), mock.patch.object(offline_packs, "upload_file", side_effect=fake_upload):
                offline_packs.upload_chunk(1, 2, work, {}, "endpoint", "bucket")
        self.assertEqual(calls, ["dem.pmtiles", "public-land.geojson", "manifest.json"])


class LogScrubTests(unittest.TestCase):
    def test_logs_do_not_contain_coordinate_strings(self):
        bbox = [-81.5901, 32.2670, -81.5632, 32.2927]
        message = offline_packs.scrub(f"failed near {bbox[0]}", bbox)
        self.assertNotIn("-81.5901", message)
        self.assertNotIn("32.2670", message)


if __name__ == "__main__":
    unittest.main()

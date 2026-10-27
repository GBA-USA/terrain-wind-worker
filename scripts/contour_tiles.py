#!/usr/bin/env python3
"""Prebuild USGS TNM contour export tiles into R2 for wind areas."""

import json
import math
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from terrain_wind import (
    buffer_bbox,
    http_bytes_response,
    log,
    r2_env,
    scrub,
    upload_file,
    validate_areas,
)

# From IversonAcres src/lib/club/elevation.ts contourTileUrl (worker uses size=512,512).
CONTOUR_EXPORT_URL = (
    "https://carto.nationalmap.gov/arcgis/rest/services/contours/MapServer/export"
)
CONTOUR_TILE_SIZE = 512
CONTOUR_ZOOM_MIN = 11
CONTOUR_ZOOM_MAX = 15
MARGIN_M = 1000
MAX_TILES_PER_RUN = 3000
MAX_OBJECT_AGE_DAYS = 90
CONCURRENCY = 4
TILE_RETRIES = 3
TILE_PREFIX = "tiles/contours/v1/512"
CACHE_CONTROL = "public, max-age=2592000"
UA = {"User-Agent": "terrain-wind-worker"}
WEB_MERCATOR_RADIUS = 6378137.0
ORIGIN_SHIFT = 20037508.342789244


def emit(name, value):
    path = os.environ.get("GITHUB_OUTPUT")
    line = f"{name}={value}\n"
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line)


def lonlat_to_tile(lon, lat, zoom):
    scale = 2**zoom
    x = int((lon + 180.0) / 360.0 * scale)
    x = max(0, min(scale - 1, x))
    lat_rad = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * scale)
    y = max(0, min(scale - 1, y))
    return x, y


def tile_bbox_wgs84(zoom, x, y):
    scale = 2**zoom
    west = x / scale * 360.0 - 180.0
    east = (x + 1) / scale * 360.0 - 180.0
    north = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / scale))))
    south = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / scale))))
    return [west, south, east, north]


def wgs84_to_web_mercator(lon, lat):
    x = lon * ORIGIN_SHIFT / 180.0
    lat = max(min(lat, 89.999999), -89.999999)
    y = math.log(math.tan((90.0 + lat) * math.pi / 360.0)) / (math.pi / 180.0)
    y = y * ORIGIN_SHIFT / 180.0
    return x, y


def tile_bbox_epsg3857(zoom, x, y):
    west, south, east, north = tile_bbox_wgs84(zoom, x, y)
    xmin, ymin = wgs84_to_web_mercator(west, south)
    xmax, ymax = wgs84_to_web_mercator(east, north)
    return xmin, ymin, xmax, ymax


def app_256_bbox_union_epsg3857(zoom, x, y):
    """Union of EPSG:3857 bboxes for the four z+1 256 px child tiles (app grid)."""
    xmin = ymin = float("inf")
    xmax = ymax = float("-inf")
    child_z = zoom + 1
    for dx in (0, 1):
        for dy in (0, 1):
            cx, cy, cz, cn = tile_bbox_epsg3857(child_z, 2 * x + dx, 2 * y + dy)
            xmin = min(xmin, cx)
            ymin = min(ymin, cy)
            xmax = max(xmax, cz)
            ymax = max(ymax, cn)
    return xmin, ymin, xmax, ymax


def contour_layers_for_z(zoom):
    if zoom <= 11:
        return "show:10,11,12,13"
    return "show:15,16,17,18"


def contour_export_params(zoom, x, y, size=CONTOUR_TILE_SIZE):
    xmin, ymin, xmax, ymax = tile_bbox_epsg3857(zoom, x, y)
    bbox = f"{xmin},{ymin},{xmax},{ymax}"
    return {
        "bbox": bbox,
        "bboxSR": "3857",
        "imageSR": "3857",
        "size": f"{size},{size}",
        "dpi": "96",
        "format": "png32",
        "transparent": "true",
        "layers": contour_layers_for_z(zoom),
        "f": "image",
    }


def contour_export_url(zoom, x, y, size=CONTOUR_TILE_SIZE):
    params = contour_export_params(zoom, x, y, size=size)
    return f"{CONTOUR_EXPORT_URL}?{urllib.parse.urlencode(params)}"


def object_key(zoom, x, y):
    return f"{TILE_PREFIX}/{zoom}/{x}/{y}.png"


def tiles_for_bbox(bbox, zoom):
    west, south, east, north = bbox
    x_min, y_max = lonlat_to_tile(west, north, zoom)
    x_max, y_min = lonlat_to_tile(east, south, zoom)
    out = []
    for x in range(min(x_min, x_max), max(x_min, x_max) + 1):
        for y in range(min(y_min, y_max), max(y_min, y_max) + 1):
            out.append((zoom, x, y))
    return out


def collect_tiles(areas, extra_tiles):
    seen = set()
    ordered = []
    skipped_cap = 0
    for area in areas:
        padded = buffer_bbox(area["bbox"], MARGIN_M)
        for zoom in range(CONTOUR_ZOOM_MIN, CONTOUR_ZOOM_MAX + 1):
            for tile in tiles_for_bbox(padded, zoom):
                if tile in seen:
                    continue
                if len(ordered) >= MAX_TILES_PER_RUN:
                    skipped_cap += 1
                    continue
                seen.add(tile)
                ordered.append(tile)
    for tile in extra_tiles:
        if not isinstance(tile, tuple) or len(tile) != 3:
            continue
        z, x, y = tile
        if z < CONTOUR_ZOOM_MIN or z > CONTOUR_ZOOM_MAX:
            continue
        if tile in seen:
            continue
        if len(ordered) >= MAX_TILES_PER_RUN:
            skipped_cap += 1
            continue
        seen.add(tile)
        ordered.append(tile)
    if skipped_cap:
        log(f"tile cap skipped {skipped_cap}")
    return ordered


def parse_miss_tiles(document):
    if not isinstance(document, dict):
        raise RuntimeError("misses response was not usable")
    raw = document.get("tiles")
    if not isinstance(raw, list):
        raise RuntimeError("misses response was not usable")
    tiles = []
    for item in raw:
        if not isinstance(item, list) or len(item) != 3:
            continue
        z, x, y = item
        if not all(isinstance(value, int) and not isinstance(value, bool) for value in (z, x, y)):
            continue
        tiles.append((z, x, y))
    return tiles


def load_misses():
    url = os.environ.get("MISSES_URL", "").strip()
    if not url:
        log("no misses source")
        return []
    token = os.environ.get("AREAS_TOKEN", "").strip()
    if not token:
        log("no misses source")
        return []
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "terrain-wind-worker",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
    except Exception:
        log("misses request failed")
        sys.exit(1)
    try:
        document = json.loads(raw)
    except json.JSONDecodeError:
        log("misses response was not usable")
        sys.exit(1)
    return parse_miss_tiles(document)


def resolve():
    out_path = Path(os.environ.get("AREAS_FILE", "areas.json"))
    test_mode = os.environ.get("TEST_MODE", "").strip().lower() == "true"
    if test_mode:
        fixture = Path(os.environ.get("FIXTURE", "fixtures/test-area.json"))
        document = json.loads(fixture.read_text(encoding="utf-8"))
    else:
        url = os.environ.get("AREAS_URL", "").strip()
        token = os.environ.get("AREAS_TOKEN", "").strip()
        if not url or not token:
            log("no areas source")
            emit("has_areas", "false")
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text('{"areas":[]}\n', encoding="utf-8")
            return
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": "terrain-wind-worker",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read()
        except Exception:
            log("areas request failed")
            sys.exit(1)
        try:
            document = json.loads(raw)
        except json.JSONDecodeError:
            log("areas response was not usable")
            sys.exit(1)
    try:
        areas = validate_areas(document)
    except RuntimeError as exc:
        log(str(exc))
        sys.exit(1)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"areas": areas}), encoding="utf-8")
    if not areas:
        emit("has_areas", "false")
        log("areas=0")
        return
    emit("has_areas", "true")
    log(f"areas={len(areas)}")


def head_object(key, env, endpoint, bucket):
    command = [
        "aws",
        "s3api",
        "head-object",
        "--bucket",
        bucket,
        "--key",
        key,
        "--endpoint-url",
        endpoint,
        "--region",
        "auto",
        "--output",
        "json",
    ]
    completed = subprocess.run(command, capture_output=True, text=True, env=env, timeout=120)
    if completed.returncode != 0:
        return None
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None


def parse_last_modified(meta, now=None):
    if not isinstance(meta, dict):
        return None
    raw = meta.get("LastModified")
    if not isinstance(raw, str):
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def object_is_fresh(key, env, endpoint, bucket, now=None):
    meta = head_object(key, env, endpoint, bucket)
    if meta is None:
        return False
    last_modified = parse_last_modified(meta, now=now)
    if last_modified is None:
        return False
    now = now or datetime.now(timezone.utc)
    if last_modified.tzinfo is None:
        last_modified = last_modified.replace(tzinfo=timezone.utc)
    return now - last_modified < timedelta(days=MAX_OBJECT_AGE_DAYS)


def is_png(data):
    return isinstance(data, (bytes, bytearray)) and data[:8] == b"\x89PNG\r\n\x1a\n"


def fetch_contour_png(zoom, x, y):
    url = contour_export_url(zoom, x, y)
    last_error = None
    for attempt in range(TILE_RETRIES):
        data, _headers, status, error = http_bytes_response(url, headers=UA, timeout=120)
        if error is None and status == 200 and is_png(data):
            return data
        last_error = error or status
        if attempt + 1 < TILE_RETRIES:
            time.sleep(2**attempt)
    raise RuntimeError(f"contour fetch failed after retries ({last_error})")


def upload_tile(zoom, x, y, body, env, endpoint, bucket):
    key = object_key(zoom, x, y)
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
        path = Path(handle.name)
        handle.write(body)
    try:
        return upload_file(
            path,
            key,
            env,
            endpoint,
            bucket,
            False,
            content_type="image/png",
            cache_control=CACHE_CONTROL,
        )
    finally:
        path.unlink(missing_ok=True)


def process_tile(tile, env, endpoint, bucket, now):
    zoom, x, y = tile
    key = object_key(zoom, x, y)
    if object_is_fresh(key, env, endpoint, bucket, now=now):
        return "skipped", 0
    body = fetch_contour_png(zoom, x, y)
    nbytes = upload_tile(zoom, x, y, body, env, endpoint, bucket)
    return "built", nbytes


def plan():
    areas_path = Path(os.environ.get("AREAS_FILE", "areas.json"))
    if not areas_path.is_file():
        log("no areas source")
        return
    areas = validate_areas(json.loads(areas_path.read_text(encoding="utf-8")))
    if not areas:
        log("no areas source")
        return
    misses = load_misses()
    tiles = collect_tiles(areas, misses)
    log(f"tiles_planned={len(tiles)} misses={len(misses)}")


def execute():
    areas_path = Path(os.environ.get("AREAS_FILE", "areas.json"))
    if not areas_path.is_file():
        log("no areas source")
        return
    areas = validate_areas(json.loads(areas_path.read_text(encoding="utf-8")))
    if not areas:
        log("no areas source")
        return

    misses = load_misses()
    tiles = collect_tiles(areas, misses)
    log(f"tiles_planned={len(tiles)}")

    storage = r2_env()
    env, endpoint, bucket = storage
    now = datetime.now(timezone.utc)

    built = 0
    skipped = 0
    failed = 0
    uploaded_bytes = 0

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = {
            pool.submit(process_tile, tile, env, endpoint, bucket, now): tile for tile in tiles
        }
        for future in as_completed(futures):
            tile = futures[future]
            try:
                status, nbytes = future.result()
                if status == "skipped":
                    skipped += 1
                else:
                    built += 1
                    uploaded_bytes += nbytes
            except Exception as exc:
                failed += 1
                log(f"tile failed: {scrub(exc, tile_bbox_wgs84(tile[0], tile[1], tile[2]))}")

    log(
        f"tiles_built={built} tiles_skipped={skipped} tiles_failed={failed} "
        f"uploaded_mb={uploaded_bytes / 1_000_000:.3f}"
    )
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(
                f"tiles_built={built} tiles_skipped={skipped} tiles_failed={failed} "
                f"uploaded_mb={uploaded_bytes / 1_000_000:.3f}\n"
            )
    if failed:
        sys.exit(1)


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else "run"
    if command == "resolve":
        resolve()
    elif command == "run":
        execute()
    elif command == "plan":
        plan()
    else:
        log("unknown command")
        sys.exit(2)


if __name__ == "__main__":
    main()

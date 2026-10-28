#!/usr/bin/env python3
"""Build z14 offline map packs (DEM PMTiles + PAD-US) and upload to R2."""

import gzip
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pmtiles.tile import Compression, TileType, zxy_to_tileid
from pmtiles.writer import write as pmtiles_write
from shapely.geometry import GeometryCollection, LineString, MultiPolygon, Polygon, mapping, shape

from terrain_wind import (
    buffer_bbox,
    http_bytes_response,
    iso,
    log,
    r2_env,
    scrub,
    upload_file,
    validate_areas,
)

# From IversonAcres src/lib/club/public-land-overlay.ts @ 97be3b3
PUBLIC_LAND_OUT_FIELDS = (
    "Unit_Nm,Mang_Name,Mang_Type,Own_Name,Des_Tp,GIS_Acres,Pub_Access,"
    "Access_Src,GIS_Src,State_Nm"
)
PADUS_QUERY_OUT_FIELDS = "OBJECTID," + PUBLIC_LAND_OUT_FIELDS
HUNTABLE_WHERE = " AND ".join(
    [
        "(Pub_Access IS NULL OR Pub_Access IN ('OA','RA','UK'))",
        (
            "(Des_Tp IS NULL OR (UPPER(Des_Tp) NOT LIKE '%NATIONAL PARK%' "
            "AND UPPER(Des_Tp) NOT LIKE '%NATIONAL PARKS%'))"
        ),
        "(IUCN_Cat IS NULL OR IUCN_Cat NOT IN ('Ia','Ib'))",
    ]
)

MAP_TILE_URL = "https://tiles.mapterhorn.com/{z}/{x}/{y}.webp"
PADUS_LAYER = (
    "https://services.arcgis.com/v01gqwM5QqNysAAi/ArcGIS/rest/services/"
    "Manager_Type_PADUS/FeatureServer/0"
)
PACK_VERSION = 1
CHUNK_Z = 14
MARGIN_M = 400
MAX_CHUNKS_PER_AREA = 64
MAX_CHUNKS_PER_RUN = 400
MANIFEST_MAX_AGE_DAYS = 30
PADUS_PAGE_SIZE = 1000
TILE_RETRIES = 3

UA = {"User-Agent": "terrain-wind-worker"}


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


def z14_tiles_for_bbox(bbox):
    west, south, east, north = bbox
    x_min, y_max = lonlat_to_tile(west, north, CHUNK_Z)
    x_max, y_min = lonlat_to_tile(east, south, CHUNK_Z)
    tiles = []
    for x in range(min(x_min, x_max), max(x_min, x_max) + 1):
        for y in range(min(y_min, y_max), max(y_min, y_max) + 1):
            tiles.append((x, y))
    return tiles


def collect_chunks(areas):
    seen = set()
    ordered = []
    skipped_cap = 0
    for area in areas:
        padded = buffer_bbox(area["bbox"], MARGIN_M)
        local = z14_tiles_for_bbox(padded)
        if len(local) > MAX_CHUNKS_PER_AREA:
            skipped_cap += len(local) - MAX_CHUNKS_PER_AREA
            local = local[:MAX_CHUNKS_PER_AREA]
        for tile in local:
            if tile in seen:
                continue
            if len(ordered) >= MAX_CHUNKS_PER_RUN:
                skipped_cap += 1
                continue
            seen.add(tile)
            ordered.append(tile)
    if skipped_cap:
        log(f"chunk cap skipped {skipped_cap}")
    return ordered


def dem_tiles_for_chunk(x14, y14):
    tiles = []
    for z in range(12, CHUNK_Z):
        factor = 2 ** (CHUNK_Z - z)
        tiles.append((z, x14 // factor, y14 // factor))
    tiles.append((CHUNK_Z, x14, y14))
    for z in range(CHUNK_Z + 1, 18):
        factor = 2 ** (z - CHUNK_Z)
        for dx in range(factor):
            for dy in range(factor):
                tiles.append((z, x14 * factor + dx, y14 * factor + dy))
    return tiles


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def pmtiles_header_value(value, enum_cls):
    if isinstance(value, enum_cls):
        return value
    if isinstance(value, int):
        return enum_cls(value)
    raise TypeError(f"expected {enum_cls.__name__} or int, got {type(value).__name__}")


def fetch_tile_bytes(z, x, y):
    url = MAP_TILE_URL.format(z=z, x=x, y=y)
    last_error = None
    for attempt in range(TILE_RETRIES):
        data, _headers, status, error = http_bytes_response(url, headers=UA, timeout=60)
        if error is None and status == 200 and data:
            return data
        if status == 404:
            return None
        last_error = error or status
        if attempt + 1 < TILE_RETRIES:
            time.sleep(2**attempt)
    raise RuntimeError(f"tile fetch failed after retries ({last_error})")


def write_dem_pmtiles(path, x14, y14):
    tiles = dem_tiles_for_chunk(x14, y14)
    with pmtiles_write(path) as writer:
        for z, x, y in tiles:
            body = fetch_tile_bytes(z, x, y)
            if body is None:
                continue
            writer.write_tile(zxy_to_tileid(z, x, y), body)
        writer.finalize(
            {
                "tile_type": pmtiles_header_value(0, TileType),
                "tile_compression": pmtiles_header_value(0, Compression),
            },
            {
                "name": "mapterhorn-dem",
                "format": "webp",
                "tileUrl": MAP_TILE_URL,
            },
        )


def padus_query_geojson(bbox):
    geometry = {
        "xmin": bbox[0],
        "ymin": bbox[1],
        "xmax": bbox[2],
        "ymax": bbox[3],
        "spatialReference": {"wkid": 4326},
    }
    features = []
    offset = 0
    while True:
        params = {
            "where": HUNTABLE_WHERE,
            "outFields": PADUS_QUERY_OUT_FIELDS,
            "geometry": json.dumps(geometry, separators=(",", ":")),
            "geometryType": "esriGeometryEnvelope",
            "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects",
            "resultOffset": str(offset),
            "resultRecordCount": str(PADUS_PAGE_SIZE),
            "f": "geojson",
        }
        url = f"{PADUS_LAYER}/query?{urllib.parse.urlencode(params)}"
        data, _headers, status, error = http_bytes_response(url, headers=UA, timeout=120)
        if error is not None or status != 200:
            raise RuntimeError("PAD-US query failed")
        payload = json.loads(data.decode("utf-8"))
        if payload.get("error"):
            raise RuntimeError("PAD-US query returned an error")
        batch = payload.get("features") or []
        features.extend(batch)
        if len(batch) < PADUS_PAGE_SIZE:
            break
        offset += len(batch)
    return {"type": "FeatureCollection", "features": features}


def _make_valid_geom(geom):
    try:
        from shapely import make_valid
    except ImportError:
        make_valid = None
    if make_valid is not None:
        return make_valid(geom)
    return geom.buffer(0)


def _polygonal_parts(geom):
    if geom.is_empty:
        return geom
    if isinstance(geom, (Polygon, MultiPolygon)):
        return geom
    if isinstance(geom, GeometryCollection):
        polys = []
        for part in geom.geoms:
            if isinstance(part, Polygon):
                polys.append(part)
            elif isinstance(part, MultiPolygon):
                polys.extend(part.geoms)
        if not polys:
            return GeometryCollection()
        if len(polys) == 1:
            return polys[0]
        return MultiPolygon(polys)
    return GeometryCollection()


def _prepare_geom_for_clip(geom):
    if geom.is_valid:
        return geom
    return _polygonal_parts(_make_valid_geom(geom))


def clip_bbox_geom(geom, bbox):
    west, south, east, north = bbox
    clip_rect = shape(
        {
            "type": "Polygon",
            "coordinates": [
                [
                    [west, south],
                    [east, south],
                    [east, north],
                    [west, north],
                    [west, south],
                ]
            ],
        }
    )
    work = _prepare_geom_for_clip(geom) if not geom.is_valid else geom
    if work.is_empty:
        return work
    return work.intersection(clip_rect)


def public_land_features(geojson, bbox):
    out = []
    skipped = 0
    for feature in geojson.get("features") or []:
        try:
            props = dict(feature.get("properties") or {})
            geom = shape(feature.get("geometry") or {})
            if geom.is_empty:
                continue
            clipped = clip_bbox_geom(geom, bbox)
            if clipped.is_empty:
                continue
            if clipped.geom_type == "Polygon":
                polys = [clipped]
            elif clipped.geom_type == "MultiPolygon":
                polys = list(clipped.geoms)
            else:
                continue
            for poly in polys:
                if poly.is_empty:
                    continue
                fill_props = {**props, "role": "fill"}
                out.append({"type": "Feature", "properties": fill_props, "geometry": mapping(poly)})
                edge = clip_bbox_geom(LineString(poly.exterior.coords), bbox)
                if edge.is_empty or edge.length == 0:
                    continue
                if edge.geom_type == "LineString":
                    lines = [edge]
                elif edge.geom_type == "MultiLineString":
                    lines = list(edge.geoms)
                else:
                    lines = []
                for segment in lines:
                    edge_props = {**props, "role": "edge"}
                    out.append(
                        {
                            "type": "Feature",
                            "properties": edge_props,
                            "geometry": mapping(segment),
                        }
                    )
        except Exception:
            skipped += 1
            continue
    if skipped:
        log(f"public_land_geom_skipped={skipped}")
    return out


def manifest_for_chunk(x, y, bbox, built, dem_path, land_path, feature_count):
    dem_bytes = dem_path.stat().st_size
    land_bytes = land_path.stat().st_size
    files = [
        {
            "name": "dem.pmtiles",
            "bytes": dem_bytes,
            "sha256": sha256_file(dem_path),
            "zooms": [12, 17],
            "tileUrl": MAP_TILE_URL,
        },
        {
            "name": "public-land.geojson",
            "bytes": land_bytes,
            "sha256": sha256_file(land_path),
            "features": feature_count,
        },
    ]
    total = dem_bytes + land_bytes
    return {
        "v": PACK_VERSION,
        "chunk": [CHUNK_Z, x, y],
        "bbox": bbox,
        "built": built,
        "files": files,
        "bytes": total,
        "attribution": ["Relief: Mapterhorn", "PAD-US: USGS"],
    }


def parse_manifest_built(manifest):
    try:
        built = manifest.get("built")
        if not isinstance(built, str):
            return None
        return datetime.strptime(built, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def manifest_is_fresh(manifest, now=None):
    if not isinstance(manifest, dict):
        return False
    if manifest.get("v") != PACK_VERSION:
        return False
    built = parse_manifest_built(manifest)
    if built is None:
        return False
    now = now or datetime.now(timezone.utc)
    return now - built < timedelta(days=MANIFEST_MAX_AGE_DAYS)


def download_manifest(key, env, endpoint, bucket):
    tmp = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "pack-manifest-check.json"
    command = [
        "aws",
        "s3",
        "cp",
        f"s3://{bucket}/{key}",
        str(tmp),
        "--endpoint-url",
        endpoint,
        "--region",
        "auto",
        "--only-show-errors",
    ]
    completed = subprocess.run(command, capture_output=True, text=True, env=env, timeout=120)
    if completed.returncode != 0:
        return None
    try:
        return json.loads(tmp.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    finally:
        tmp.unlink(missing_ok=True)


def should_skip_chunk(x, y, env, endpoint, bucket, now=None):
    key = f"packs/v1/z14/{x}/{y}/manifest.json"
    manifest = download_manifest(key, env, endpoint, bucket)
    return manifest_is_fresh(manifest, now=now)


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


def build_chunk(x, y, work_dir):
    dem_path = work_dir / "dem.pmtiles"
    land_path = work_dir / "public-land.geojson"
    bbox = tile_bbox_wgs84(CHUNK_Z, x, y)
    built = iso(datetime.now(timezone.utc).replace(microsecond=0))

    write_dem_pmtiles(dem_path, x, y)
    raw_land = padus_query_geojson(bbox)
    features = public_land_features(raw_land, bbox)
    land_body = json.dumps({"type": "FeatureCollection", "features": features}, separators=(",", ":"))
    land_path.write_text(land_body, encoding="utf-8")

    manifest = manifest_for_chunk(x, y, bbox, built, dem_path, land_path, len(features))
    manifest_path = work_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    local_bytes = dem_path.stat().st_size + land_path.stat().st_size + manifest_path.stat().st_size
    return manifest, local_bytes, dem_path, land_path, manifest_path


def upload_chunk(x, y, work_dir, env, endpoint, bucket):
    prefix = f"packs/v1/z14/{x}/{y}"
    manifest, _local_bytes, dem_path, land_path, manifest_path = build_chunk(x, y, work_dir)

    total = 0
    total += upload_file(
        dem_path,
        f"{prefix}/dem.pmtiles",
        env,
        endpoint,
        bucket,
        False,
        content_type="application/vnd.pmtiles",
    )
    total += upload_file(
        land_path,
        f"{prefix}/public-land.geojson",
        env,
        endpoint,
        bucket,
        True,
        content_type="application/geo+json",
    )
    total += upload_file(
        manifest_path,
        f"{prefix}/manifest.json",
        env,
        endpoint,
        bucket,
        False,
        content_type="application/json",
        cache_control="no-cache",
    )
    return total, manifest


def execute():
    from run_status import RunStatusReporter

    status = RunStatusReporter("offline-packs")
    status.start()
    conclusion = "success"
    exit_code = 0
    status_fields = {
        "chunks_built": 0,
        "chunks_skipped": 0,
        "chunks_failed": 0,
        "uploaded_mb": 0.0,
    }
    try:
        areas_path = Path(os.environ.get("AREAS_FILE", "areas.json"))
        if not areas_path.is_file():
            log("no areas source")
            return
        areas = validate_areas(json.loads(areas_path.read_text(encoding="utf-8")))
        if not areas:
            log("no areas source")
            return

        test_mode = os.environ.get("TEST_MODE", "").strip().lower() == "true"
        if test_mode:
            env, endpoint, bucket = None, None, None
        else:
            storage = r2_env()
            env, endpoint, bucket = storage
        chunks = collect_chunks(areas)
        if test_mode:
            chunks = chunks[:1]
        log(f"chunks_planned={len(chunks)}")

        built = 0
        skipped = 0
        failed = 0
        uploaded_bytes = 0
        work_root = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "offline-packs"

        for x, y in chunks:
            if not test_mode and should_skip_chunk(x, y, env, endpoint, bucket):
                skipped += 1
                continue
            work_dir = work_root / f"{x}-{y}"
            if work_dir.exists():
                shutil.rmtree(work_dir)
            work_dir.mkdir(parents=True)
            try:
                if test_mode:
                    _manifest, chunk_bytes, *_paths = build_chunk(x, y, work_dir)
                else:
                    chunk_bytes, _manifest = upload_chunk(x, y, work_dir, env, endpoint, bucket)
                uploaded_bytes += chunk_bytes
                built += 1
                log(f"chunk built bytes={chunk_bytes}")
            except Exception as exc:
                failed += 1
                log(f"chunk failed: {scrub(exc, tile_bbox_wgs84(CHUNK_Z, x, y))}")
            finally:
                shutil.rmtree(work_dir, ignore_errors=True)

        status_fields["chunks_built"] = built
        status_fields["chunks_skipped"] = skipped
        status_fields["chunks_failed"] = failed
        status_fields["uploaded_mb"] = round(uploaded_bytes / 1_000_000.0, 3)

        log(
            f"chunks_built={built} chunks_skipped={skipped} chunks_failed={failed} "
            f"uploaded_mb={uploaded_bytes / 1_000_000:.3f}"
        )
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as handle:
                handle.write(
                    f"chunks_built={built} chunks_skipped={skipped} chunks_failed={failed} "
                    f"uploaded_mb={uploaded_bytes / 1_000_000:.3f}\n"
                )
        if failed:
            exit_code = 1
            conclusion = "partial" if built else "failure"
    finally:
        status.finish(conclusion, **status_fields)
    if exit_code:
        sys.exit(exit_code)


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else "run"
    if command == "resolve":
        resolve()
    elif command == "run":
        execute()
    else:
        log("unknown command")
        sys.exit(2)


if __name__ == "__main__":
    main()

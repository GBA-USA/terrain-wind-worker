#!/usr/bin/env python3
"""Fetch areas, run WindNinja, and publish hourly u/v grids to R2.

Areas are runtime input only. This script does not print secrets or bboxes.
"""

import gzip
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HRRR_BASE = "https://noaa-hrrr-bdp-pds.s3.amazonaws.com"
DEM_URL = (
    "https://elevation.nationalmap.gov/arcgis/rest/services/"
    "3DEPElevation/ImageServer/exportImage"
)
FORECAST_HOURS = range(1, 19)
# Run-stamped objects never change; latest.json and coverage.json move every run (3 h cadence).
CACHE_IMMUTABLE = "public, max-age=31536000, immutable"
CACHE_LATEST = "public, max-age=300"
HRRR_SUBSET_VARIABLES = frozenset(
    {"TMP", "UGRD", "VGRD", "TCDC", "LCDC", "RH", "DPT", "WIND", "PRATE", "DSWRF"}
)
# Scalar weather keys match IversonAcres src/lib/club/scent-weather.server.ts.
SCENT_WEATHER_KEYS = (
    "cloud_pct",
    "cloud_low_pct",
    "relative_humidity_pct",
    "dew_point_c",
    "temperature_c",
    "gust_mph",
    "precip_mm_per_h",
    "shortwave_wm2",
    "weather_code",
)
MPS_TO_MPH = 2.2369362920544
MAX_EDGE_KM = 40
MAX_EDGE_KM_COARSE = 80
TILE_EPS = 1e-9
TILE_CAP = 60
TILE_BUDGET = 60.0
TILE_COST_100 = 1.0
TILE_COST_200 = 0.5
ALLOWED_CELL_M = frozenset({100, 200})
DEM_BUFFER_M = 800
CELL_M = 100
UA = {"User-Agent": "terrain-wind-worker"}
ASC_HEADER = {
    "ncols",
    "nrows",
    "xllcorner",
    "yllcorner",
    "xllcenter",
    "yllcenter",
    "cellsize",
    "dx",
    "dy",
    "nodata_value",
}


def log(message):
    print(message, flush=True)


def emit(name, value):
    path = os.environ.get("GITHUB_OUTPUT")
    line = f"{name}={value}\n"
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line)
    else:
        print(line, end="")


def http_bytes(url, headers=None, timeout=120):
    req_headers = dict(UA)
    if headers:
        req_headers.update(headers)
    request = urllib.request.Request(url, headers=req_headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(), response.headers


def http_bytes_response(url, headers=None, timeout=120):
    req_headers = dict(UA)
    if headers:
        req_headers.update(headers)
    request = urllib.request.Request(url, headers=req_headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read(), response.headers, response.status, None
    except urllib.error.HTTPError as exc:
        return exc.read(), exc.headers, exc.code, exc
    except Exception as exc:
        return b"", None, None, exc


def response_content_type(headers):
    if headers is None:
        return "unknown"
    if hasattr(headers, "get_content_type"):
        return headers.get_content_type()
    return headers.get("Content-Type", "unknown")


def log_failed_http_attempt(label, attempt, data, headers, status, error):
    if status is not None:
        detail = f"HTTP {status}"
    elif error is not None:
        detail = type(error).__name__
    else:
        detail = "error"
    ctype = response_content_type(headers)
    log(f"{label} attempt {attempt + 1} failed: {detail} content-type={ctype} bytes={len(data)}")


TIFF_MAGIC = (b"II*\x00", b"MM\x00*")
HTTP_RETRY_BACKOFFS = (5, 15)


def http_bytes_retry(
    label,
    url,
    *,
    url_for_attempt=None,
    headers=None,
    timeout=120,
    attempts=3,
    backoffs=HTTP_RETRY_BACKOFFS,
    validate=None,
    fail_message=None,
):
    for attempt in range(attempts):
        request_url = url_for_attempt(attempt) if url_for_attempt is not None else url
        data, resp_headers, status, error = http_bytes_response(
            request_url,
            headers=headers,
            timeout=timeout,
        )
        if error is None and (validate is None or validate(data)):
            return data, resp_headers
        log_failed_http_attempt(label, attempt, data, resp_headers, status, error)
        if attempt + 1 >= attempts:
            break
        delay = backoffs[attempt] if attempt < len(backoffs) else backoffs[-1]
        log(f"{label} retry {attempt + 1}, waiting {delay}s")
        time.sleep(delay)
    raise RuntimeError(fail_message or f"{label} failed")


def content_length(url):
    request = urllib.request.Request(url, method="HEAD", headers=UA)
    with urllib.request.urlopen(request, timeout=60) as response:
        return int(response.headers["Content-Length"])


def http_ok(url):
    request = urllib.request.Request(url, method="HEAD", headers=UA)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return 200 <= response.status < 300
    except urllib.error.HTTPError:
        return False


def run(cmd, env=None, timeout=300):
    completed = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )
    if completed.returncode != 0:
        tail = []
        for line in (completed.stderr or completed.stdout or "").splitlines():
            lower = line.lower()
            if "bbox" in lower or "latitude" in lower or "longitude" in lower:
                continue
            if "r2.cloudflarestorage" in lower or "x-amz-" in lower:
                continue
            text = line.strip()
            if text:
                tail.append(text)
        detail = " | ".join(tail[-6:])[:500]
        raise RuntimeError(f"{Path(cmd[0]).name} failed" + (f": {detail}" if detail else ""))
    return completed


def span_km(bbox):
    min_lon, min_lat, max_lon, max_lat = bbox
    lat = math.radians((min_lat + max_lat) / 2.0)
    width = (max_lon - min_lon) * 111.32 * math.cos(lat)
    height = (max_lat - min_lat) * 111.32
    return width, height


def normalize_cell_m(raw):
    if raw is None:
        return CELL_M
    if isinstance(raw, bool):
        log("invalid cell_m, using 100")
        return CELL_M
    try:
        value = int(raw)
    except (TypeError, ValueError):
        log("invalid cell_m, using 100")
        return CELL_M
    if value in ALLOWED_CELL_M:
        return value
    log("invalid cell_m, using 100")
    return CELL_M


def max_edge_km(cell_m):
    return MAX_EDGE_KM_COARSE if cell_m == 200 else MAX_EDGE_KM


def tile_budget_cost(cell_m):
    return TILE_COST_200 if cell_m == 200 else TILE_COST_100


def validate_areas(document, stats=None):
    if not isinstance(document, dict) or not isinstance(document.get("areas"), list):
        raise RuntimeError("areas response was not usable")
    areas = []
    if stats is not None:
        stats.setdefault("areas_oversized", 0)
        stats.setdefault("areas_coarse", 0)
    for item in document["areas"]:
        if not isinstance(item, dict):
            log("skipped invalid area")
            continue
        key = item.get("key")
        bbox = item.get("bbox")
        if not isinstance(key, str) or not key or len(key) > 128:
            log("skipped invalid area key")
            continue
        if any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-" for ch in key):
            log("skipped invalid area key")
            continue
        if (
            not isinstance(bbox, list)
            or len(bbox) != 4
            or not all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in bbox)
        ):
            log(f"area {key} skipped: bad bbox")
            continue
        min_lon, min_lat, max_lon, max_lat = (float(value) for value in bbox)
        if not (-180.0 <= min_lon < max_lon <= 180.0 and -90.0 <= min_lat < max_lat <= 90.0):
            log(f"area {key} skipped: bad bbox")
            continue
        cell_m = normalize_cell_m(item.get("cell_m"))
        if stats is not None and cell_m == 200:
            stats["areas_coarse"] += 1
        edge_limit = max_edge_km(cell_m)
        width_km, height_km = span_km((min_lon, min_lat, max_lon, max_lat))
        if width_km > edge_limit or height_km > edge_limit:
            if stats is not None:
                stats["areas_oversized"] += 1
            log(f"area {key} skipped: domain exceeds {edge_limit} km")
            continue
        areas.append(
            {"key": key, "bbox": [min_lon, min_lat, max_lon, max_lat], "cell_m": cell_m}
        )
    return areas


def tile_indices_for_point(lat, lng):
    return (
        math.floor(lat * 10 + TILE_EPS),
        math.floor(lng * 10 + TILE_EPS),
    )


def tile_key(lat_i, lng_i):
    return f"t{lat_i}_{lng_i}"


def tile_bbox(lat_i, lng_i):
    return [lng_i / 10, lat_i / 10, (lng_i + 1) / 10, (lat_i + 1) / 10]


def tiles_for_bbox(bbox):
    min_lon, min_lat, max_lon, max_lat = bbox
    lat_min = math.floor(min_lat * 10 + TILE_EPS)
    lat_max = math.ceil(max_lat * 10 - TILE_EPS) - 1
    lng_min = math.floor(min_lon * 10 + TILE_EPS)
    lng_max = math.ceil(max_lon * 10 - TILE_EPS) - 1
    tiles = {}
    for lat_i in range(lat_min, lat_max + 1):
        for lng_i in range(lng_min, lng_max + 1):
            key = tile_key(lat_i, lng_i)
            tiles[key] = tile_bbox(lat_i, lng_i)
    return tiles


def areas_from_tiles(tile_map):
    return [{"key": key, "bbox": tile_map[key]} for key in sorted(tile_map)]


def expand_areas_to_tiles(areas, capped_out=None):
    tile_meta = {}
    for area in areas:
        cell_m = area.get("cell_m", CELL_M)
        for key, bbox in tiles_for_bbox(area["bbox"]).items():
            if key not in tile_meta:
                tile_meta[key] = {"bbox": bbox, "cell_m": cell_m}
            else:
                tile_meta[key]["cell_m"] = min(tile_meta[key]["cell_m"], cell_m)
    ordered = sorted(
        tile_meta.items(),
        key=lambda item: (0 if item[1]["cell_m"] == CELL_M else 1, item[0]),
    )
    selected = []
    capped = []
    budget_used = 0.0
    for key, info in ordered:
        cost = tile_budget_cost(info["cell_m"])
        if budget_used + cost <= TILE_BUDGET + 1e-9:
            selected.append({"key": key, "bbox": info["bbox"], "cell_m": info["cell_m"]})
            budget_used += cost
        else:
            capped.append(key)
    if capped:
        log(f"tile cap reached, skipped {len(capped)}")
    if capped_out is not None:
        capped_out.clear()
        capped_out.extend(capped)
    return selected


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
            emit("cache_key", "none")
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
        area_stats = {"areas_oversized": 0, "areas_coarse": 0}
        areas = validate_areas(document, area_stats)
    except RuntimeError as exc:
        log(str(exc))
        sys.exit(1)
    capped_keys = []
    if not test_mode:
        areas = expand_areas_to_tiles(areas, capped_out=capped_keys)
    areas = filter_areas_by_tiles(areas, tile_filter_from_env())
    wind_meta = {
        "areas_oversized": area_stats["areas_oversized"],
        "areas_coarse": area_stats["areas_coarse"],
        "tiles_capped": capped_keys,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps({"areas": areas, "wind_meta": wind_meta}),
        encoding="utf-8",
    )
    if not areas:
        emit("has_areas", "false")
        emit("cache_key", "none")
        log("areas=0")
        return
    keys = "\n".join(sorted(area["key"] for area in areas))
    stamp = json.dumps(areas, separators=(",", ":"), sort_keys=True)
    digest = hashlib.sha256(keys.encode()).hexdigest()[:12]
    stamp_hex = hashlib.sha256(stamp.encode()).hexdigest()[:12]
    emit("has_areas", "true")
    emit("cache_key", f"{digest}-{stamp_hex}")
    log(f"areas={len(areas)}")


def utm_epsg(lon, lat):
    zone = int((lon + 180.0) / 6.0) + 1
    return (32600 if lat >= 0 else 32700) + zone


def buffer_bbox(bbox, meters):
    min_lon, min_lat, max_lon, max_lat = bbox
    lat = math.radians((min_lat + max_lat) / 2.0)
    dlat = meters / 111320.0
    dlon = meters / (111320.0 * max(math.cos(lat), 0.2))
    return [min_lon - dlon, min_lat - dlat, max_lon + dlon, max_lat + dlat]


def download_dem(bbox, dest):
    min_lon, min_lat, max_lon, max_lat = buffer_bbox(bbox, DEM_BUFFER_M)
    lat = math.radians((min_lat + max_lat) / 2.0)
    width = max(2, int(round((max_lon - min_lon) * 111320.0 * math.cos(lat) / 30.0)))
    height = max(2, int(round((max_lat - min_lat) * 111320.0 / 30.0)))
    width = min(width, 2500)
    height = min(height, 2500)
    base_params = {
        "bbox": f"{min_lon},{min_lat},{max_lon},{max_lat}",
        "bboxSR": "4326",
        "imageSR": "4326",
        "format": "tiff",
        "pixelType": "F32",
        "interpolation": "RSP_BilinearInterpolation",
        "f": "image",
    }

    def dem_url_for_attempt(attempt):
        params = dict(base_params)
        shrink = attempt
        params["size"] = f"{max(2, width - shrink)},{max(2, height - shrink)}"
        if attempt > 0:
            params["_cb"] = str(attempt)
        return f"{DEM_URL}?{urllib.parse.urlencode(params)}"

    data, _headers = http_bytes_retry(
        "DEM download",
        dem_url_for_attempt(0),
        url_for_attempt=dem_url_for_attempt,
        timeout=180,
        validate=lambda body: body[:4] in TIFF_MAGIC,
        fail_message="DEM download failed",
    )
    raw = dest.with_suffix(".ll.tif")
    raw.write_bytes(data)
    epsg = utm_epsg((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)
    run(
        [
            "gdalwarp",
            "-q",
            "-overwrite",
            "-t_srs",
            f"EPSG:{epsg}",
            "-tr",
            "100",
            "100",
            "-r",
            "bilinear",
            "-dstnodata",
            "-9999",
            "-ot",
            "Float32",
            str(raw),
            str(dest),
        ]
    )
    raw.unlink(missing_ok=True)
    filler = shutil.which("gdal_fillnodata.py") or shutil.which("gdal_fillnodata")
    if filler:
        filled = dest.with_suffix(".filled.tif")
        try:
            run([filler, "-md", "20", "-si", "0", str(dest), str(filled)])
            filled.replace(dest)
        except RuntimeError:
            log("DEM nodata fill skipped")
            filled.unlink(missing_ok=True)
    return epsg


def dem_for(area, cache_dir):
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = area["key"]
    dest = cache_dir / f"{key}.tif"
    stamp_path = cache_dir / f"{key}.stamp"
    stamp = hashlib.sha256(json.dumps(area["bbox"]).encode()).hexdigest()
    if dest.is_file() and stamp_path.is_file() and stamp_path.read_text(encoding="utf-8").strip() == stamp:
        return dest
    log(f"area {key} fetching DEM")
    download_dem(area["bbox"], dest)
    stamp_path.write_text(stamp + "\n", encoding="utf-8")
    return dest


def latest_hrrr_cycle():
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    for back in range(1, 12):
        cycle = now - timedelta(hours=back)
        url = hrrr_object(cycle, 18) + ".idx"
        if http_ok(url):
            return cycle
    raise RuntimeError("no HRRR cycle available")


def hrrr_cycle_from_env():
    raw = os.environ.get("TERRAIN_WIND_HRRR_CYCLE", "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        raise RuntimeError("TERRAIN_WIND_HRRR_CYCLE is not YYYY-MM-DDTHH:MM:SSZ") from None


def resolve_hrrr_cycle():
    cycle = hrrr_cycle_from_env()
    return cycle if cycle is not None else latest_hrrr_cycle()


def hrrr_object(cycle, forecast_hour):
    ymd = cycle.strftime("%Y%m%d")
    hour = cycle.strftime("%H")
    return (
        f"{HRRR_BASE}/hrrr.{ymd}/conus/"
        f"hrrr.t{hour}z.wrfsfcf{forecast_hour:02d}.grib2"
    )


def parse_hrrr_idx_rows(index_text):
    rows = []
    for line in index_text.splitlines():
        parts = line.split(":")
        if len(parts) < 6:
            continue
        try:
            start = int(parts[1])
        except ValueError:
            continue
        rows.append(
            {
                "start": start,
                "variable": parts[3],
                "level": parts[4],
                "desc": parts[5] if len(parts) > 5 else "",
            }
        )
    return rows


def hrrr_message_selected(row):
    variable = row["variable"]
    level = row["level"]
    desc = row["desc"]
    if variable == "TMP" and level.startswith("2 m above ground") and "max" not in desc:
        return True
    if variable in ("UGRD", "VGRD") and level.startswith("10 m above ground") and "max" not in desc:
        return True
    if variable == "WIND" and level.startswith("10 m above ground") and "max" in desc:
        return True
    if variable == "TCDC" and level == "entire atmosphere":
        return True
    if variable == "LCDC" and level == "low cloud layer":
        return True
    if variable in ("RH", "DPT") and level.startswith("2 m above ground"):
        return True
    if variable in ("PRATE", "DSWRF") and level.startswith("surface"):
        return True
    return False


def hrrr_subset_ranges(rows, file_size):
    wanted = []
    for index, row in enumerate(rows):
        if not hrrr_message_selected(row):
            continue
        end = rows[index + 1]["start"] - 1 if index + 1 < len(rows) else file_size - 1
        wanted.append((row["variable"], row["start"], end))
    names = {item[0] for item in wanted}
    if names != set(HRRR_SUBSET_VARIABLES):
        missing = sorted(set(HRRR_SUBSET_VARIABLES) - names)
        raise RuntimeError(f"HRRR subset is missing required fields: {', '.join(missing)}")
    return wanted


def fetch_hrrr_subset(cycle, forecast_hour, dest):
    url = hrrr_object(cycle, forecast_hour)
    try:
        index_text = http_bytes(url + ".idx", timeout=60)[0].decode("utf-8", "replace")
        size = content_length(url)
    except Exception:
        raise RuntimeError("HRRR download failed") from None
    rows = parse_hrrr_idx_rows(index_text)
    wanted = hrrr_subset_ranges(rows, size)
    order = {
        "TMP": 0,
        "DPT": 1,
        "RH": 2,
        "UGRD": 3,
        "VGRD": 4,
        "WIND": 5,
        "PRATE": 6,
        "TCDC": 7,
        "LCDC": 8,
        "DSWRF": 9,
    }
    blob = b""
    try:
        for _variable, start, end in sorted(wanted, key=lambda item: order[item[0]]):
            chunk, _headers = http_bytes(url, headers={"Range": f"bytes={start}-{end}"}, timeout=180)
            if not chunk.startswith(b"GRIB"):
                raise RuntimeError("HRRR byte range was not a GRIB message")
            blob += chunk
    except RuntimeError:
        raise
    except Exception:
        raise RuntimeError("HRRR download failed") from None
    dest.write_bytes(blob)
    return cycle + timedelta(hours=forecast_hour), len(blob)


def fetch_hrrr_subset_cached(cycle, forecast_hour, dest, cache_dir=None):
    """Same bytes as fetch_hrrr_subset; tiles in one job share one download per hour."""
    if cache_dir is None:
        return fetch_hrrr_subset(cycle, forecast_hour, dest)
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / f"{cycle.strftime('%Y%m%d%H')}-f{forecast_hour:02d}.grib2"
    valid = cycle + timedelta(hours=forecast_hour)
    if not cached.is_file():
        partial = cache_dir / f"{cached.name}.{os.getpid()}.part"
        try:
            fetch_hrrr_subset(cycle, forecast_hour, partial)
            os.replace(partial, cached)
        finally:
            partial.unlink(missing_ok=True)
    shutil.copyfile(cached, dest)
    return valid, cached.stat().st_size


def read_asc(path):
    header = {}
    tokens = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if not parts:
            continue
        key = parts[0].lower()
        if key in ASC_HEADER and key not in header:
            header[key] = parts[1]
            if len(parts) > 2:
                tokens.extend(parts[2:])
        else:
            tokens.extend(parts)
    ncols = int(float(header["ncols"]))
    nrows = int(float(header["nrows"]))
    nodata = float(header.get("nodata_value", "-9999"))
    values = [float(token) for token in tokens]
    if len(values) != ncols * nrows:
        raise RuntimeError("ASCII grid size does not match its header")
    return ncols, nrows, nodata, values


def write_asc(path, ncols, nrows, xll, yll, cell, values):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(f"ncols {ncols}\n")
        handle.write(f"nrows {nrows}\n")
        handle.write(f"xllcorner {xll:.6f}\n")
        handle.write(f"yllcorner {yll:.6f}\n")
        handle.write(f"cellsize {cell:.6f}\n")
        handle.write("NODATA_value -9999\n")
        for row in range(nrows):
            start = row * ncols
            handle.write(" ".join(f"{value:.4f}" for value in values[start : start + ncols]))
            handle.write("\n")


def gdal_info(path):
    completed = subprocess.run(
        ["gdalinfo", "-json", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def finite_mean(values, nodata):
    good = [value for value in values if math.isfinite(value) and abs(value - nodata) > 0.1 and value > -9000]
    if not good:
        raise RuntimeError("forecast sample was empty")
    return sum(good) / len(good)


def sample_band_mean(grib, band, bbox, work):
    tif = work / f"mean-{band}.tif"
    asc = work / f"mean-{band}.asc"
    run(
        [
            "gdal_translate",
            "-q",
            "-b",
            str(band),
            str(grib),
            str(work / f"band-{band}.tif"),
        ]
    )
    run(
        [
            "gdalwarp",
            "-q",
            "-overwrite",
            "-t_srs",
            "EPSG:4326",
            "-te",
            str(bbox[0]),
            str(bbox[1]),
            str(bbox[2]),
            str(bbox[3]),
            "-ts",
            "12",
            "12",
            "-r",
            "bilinear",
            "-dstnodata",
            "-9999",
            str(work / f"band-{band}.tif"),
            str(tif),
        ]
    )
    run(["gdal_translate", "-q", "-of", "AAIGrid", str(tif), str(asc)])
    _ncols, _nrows, nodata, values = read_asc(asc)
    return finite_mean(values, nodata)


def band_number(info, comment_prefix, short_name):
    for band in info.get("bands", []):
        metadata = (band.get("metadata") or {}).get("") or {}
        comment = metadata.get("GRIB_COMMENT") or ""
        short = metadata.get("GRIB_SHORT_NAME") or ""
        if comment.startswith(comment_prefix) and short == short_name:
            return band["band"]
    raise RuntimeError("HRRR subset is missing a required field")


def met_direction(u_value, v_value):
    if u_value == 0.0 and v_value == 0.0:
        return 0.0
    return (270.0 - math.degrees(math.atan2(v_value, u_value))) % 360.0


def build_speed_dir(grib, dem, bbox, work):
    info = gdal_info(grib)
    u_band = band_number(info, "u-component of wind", "10-HTGL")
    v_band = band_number(info, "v-component of wind", "10-HTGL")
    dem_info = gdal_info(dem)
    width, height = dem_info["size"]
    origin_x, pixel_w, _rot_x, origin_y, _rot_y, pixel_h = dem_info["geoTransform"]
    xmin = origin_x
    ymax = origin_y
    xmax = xmin + width * pixel_w
    ymin = ymax + height * pixel_h
    epsg = utm_epsg((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)
    arrays = {}
    for name, band in (("u", u_band), ("v", v_band)):
        src = work / f"{name}.tif"
        warped = work / f"{name}-dem.tif"
        asc = work / f"{name}-dem.asc"
        run(["gdal_translate", "-q", "-b", str(band), str(grib), str(src)])
        run(
            [
                "gdalwarp",
                "-q",
                "-overwrite",
                "-t_srs",
                f"EPSG:{epsg}",
                "-te",
                str(xmin),
                str(ymin),
                str(xmax),
                str(ymax),
                "-ts",
                str(width),
                str(height),
                "-r",
                "bilinear",
                "-dstnodata",
                "-9999",
                str(src),
                str(warped),
            ]
        )
        run(["gdal_translate", "-q", "-of", "AAIGrid", str(warped), str(asc)])
        arrays[name] = read_asc(asc)
    u_vals = arrays["u"][3]
    v_vals = arrays["v"][3]
    nodata = arrays["u"][2]
    speed = []
    direction = []
    usable_u = []
    usable_v = []
    for u_value, v_value in zip(u_vals, v_vals):
        if abs(u_value - nodata) < 0.1 or abs(v_value - nodata) < 0.1 or u_value < -9000 or v_value < -9000:
            speed.append(None)
            direction.append(None)
        else:
            usable_u.append(u_value)
            usable_v.append(v_value)
            speed.append(math.hypot(u_value, v_value))
            direction.append(met_direction(u_value, v_value))
    if not usable_u:
        raise RuntimeError("forecast sample was empty")
    fill_speed = sum(math.hypot(u, v) for u, v in zip(usable_u, usable_v)) / len(usable_u)
    mean_u = sum(usable_u) / len(usable_u)
    mean_v = sum(usable_v) / len(usable_v)
    fill_dir = met_direction(mean_u, mean_v)
    speed = [fill_speed if value is None else value for value in speed]
    direction = [fill_dir if value is None else value for value in direction]
    speed_path = work / "speed.asc"
    dir_path = work / "dir.asc"
    write_asc(speed_path, width, height, xmin, ymin, abs(pixel_w), speed)
    write_asc(dir_path, width, height, xmin, ymin, abs(pixel_w), direction)
    return speed_path, dir_path, fill_speed


def normalize_temperature_c(value):
    if value > 150.0:
        value -= 273.15
    return round(value, 1)


def normalize_cloud_cover_percent(value):
    if value <= 1.5:
        value *= 100.0
    return round(max(0.0, min(100.0, value)), 1)


def normalize_prate_mm_per_hour(value):
    return round(max(0.0, value) * 3600.0, 2)


def round_scalar(value, places=1):
    return round(value, places)


def derive_weather_code(precip_mm_h, cloud_cover, temp_c):
    if precip_mm_h >= 1.0:
        if temp_c < 0.0:
            return 71
        if precip_mm_h >= 5.0:
            return 65
        return 61
    if precip_mm_h >= 0.1:
        return 51
    if cloud_cover >= 90.0:
        return 3
    if cloud_cover >= 50.0:
        return 2
    if cloud_cover >= 20.0:
        return 1
    return 0


def scent_weather_payload(
    cloud_pct,
    cloud_low_pct,
    relative_humidity_pct,
    dew_point_c,
    temperature_c,
    gust_mps,
    precip_rate,
    shortwave_wm2,
):
    cloud_pct = normalize_cloud_cover_percent(cloud_pct)
    cloud_low_pct = normalize_cloud_cover_percent(cloud_low_pct)
    temperature_c = normalize_temperature_c(temperature_c)
    dew_point_c = normalize_temperature_c(dew_point_c)
    precip_mm_per_h = normalize_prate_mm_per_hour(precip_rate)
    gust_mph = round_scalar(gust_mps * MPS_TO_MPH)
    payload = {
        "cloud_pct": cloud_pct,
        "cloud_low_pct": cloud_low_pct,
        "relative_humidity_pct": round_scalar(relative_humidity_pct),
        "dew_point_c": dew_point_c,
        "temperature_c": temperature_c,
        "gust_mph": gust_mph,
        "precip_mm_per_h": precip_mm_per_h,
        "shortwave_wm2": round_scalar(shortwave_wm2),
        "weather_code": derive_weather_code(precip_mm_per_h, cloud_pct, temperature_c),
    }
    for key in SCENT_WEATHER_KEYS:
        if key not in payload:
            raise RuntimeError("scent weather payload is incomplete")
    return payload


def weather_from_grib(grib, bbox, work):
    info = gdal_info(grib)
    temp = sample_band_mean(grib, band_number(info, "Temperature", "2-HTGL"), bbox, work)
    dew = sample_band_mean(grib, band_number(info, "Dew point temperature", "2-HTGL"), bbox, work)
    rh = sample_band_mean(grib, band_number(info, "Relative humidity", "2-HTGL"), bbox, work)
    gust = sample_band_mean(grib, band_number(info, "Wind speed", "10-HTGL"), bbox, work)
    precip = sample_band_mean(grib, band_number(info, "Precipitation rate", "0-SFC"), bbox, work)
    cloud = sample_band_mean(grib, band_number(info, "Total cloud cover", "0-EATM"), bbox, work)
    cloud_low = sample_band_mean(grib, band_number(info, "Low cloud cover", "0-LCY"), bbox, work)
    shortwave = sample_band_mean(grib, band_number(info, "Downward short-wave radiation flux", "0-SFC"), bbox, work)
    return scent_weather_payload(cloud, cloud_low, rh, dew, temp, gust, precip, shortwave)


def surface_means(grib, bbox, work):
    weather = weather_from_grib(grib, bbox, work)
    return weather["temperature_c"], weather["cloud_pct"]


def windninja_env(prefix):
    env = os.environ.copy()
    env["WINDNINJA_DATA"] = str(prefix / "share" / "windninja")
    env["PATH"] = str(prefix / "bin") + os.pathsep + env.get("PATH", "")
    return env


MOMENTUM_ITERATIONS = 300


def env_bool(name, default):
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes"):
        return True
    if raw in ("0", "false", "no"):
        return False
    raise RuntimeError(f"invalid boolean for {name}")


def parse_csv_tokens(name):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    tokens = [part.strip() for part in raw.split(",") if part.strip()]
    return tokens or None


def parse_utc_hours():
    tokens = parse_csv_tokens("TERRAIN_WIND_HOURS")
    if tokens is None:
        return None
    hours = set()
    for token in tokens:
        try:
            hour = int(token)
        except ValueError:
            raise RuntimeError(f"invalid UTC hour in TERRAIN_WIND_HOURS: {token}")
        if hour < 0 or hour > 23:
            raise RuntimeError(f"UTC hour out of range in TERRAIN_WIND_HOURS: {token}")
        hours.add(hour)
    return hours


def normalize_output_prefix(prefix):
    value = (prefix or "wind").strip().strip("/")
    if not value:
        value = "wind"
    if value == "wind":
        return "wind"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
        raise RuntimeError("TERRAIN_WIND_PREFIX is not a valid object key prefix")
    return value


def windninja_run_options_from_env():
    return {
        "diurnal": env_bool("TERRAIN_WIND_DIURNAL", True),
        "stability": env_bool("TERRAIN_WIND_STABILITY", True),
        "momentum": env_bool("TERRAIN_WIND_MOMENTUM", False),
        "utc_hours": parse_utc_hours(),
        "output_prefix": normalize_output_prefix(os.environ.get("TERRAIN_WIND_PREFIX", "wind")),
    }


def tile_filter_from_env():
    tokens = parse_csv_tokens("TERRAIN_WIND_TILES")
    if tokens is None:
        return None
    return set(tokens)


def filter_areas_by_tiles(areas, tile_filter):
    if not tile_filter:
        return areas
    return [area for area in areas if area["key"] in tile_filter]


def cfg_bool(value):
    return "true" if value else "false"


def build_base_cfg_pairs(dem, out_dir, options, cell_m=CELL_M):
    if cell_m not in ALLOWED_CELL_M:
        cell_m = CELL_M
    diurnal = options["diurnal"]
    stability = options["stability"]
    momentum = options["momentum"]
    pairs = [
        ("num_threads", "2"),
        ("elevation_file", dem),
        ("time_zone", "UTC"),
        ("output_wind_height", "10"),
        ("units_output_wind_height", "m"),
        ("output_speed_units", "mps"),
        ("vegetation", "grass"),
        ("mesh_resolution", str(cell_m)),
        ("units_mesh_resolution", "m"),
        ("diurnal_winds", cfg_bool(diurnal)),
        ("non_neutral_stability", cfg_bool(stability and not momentum)),
        ("write_ascii_output", "true"),
        ("ascii_out_aaigrid", "true"),
        ("ascii_out_json", "false"),
        ("ascii_out_proj", "false"),
        ("ascii_out_geog", "true"),
        ("ascii_out_uv", "true"),
        ("ascii_out_resolution", str(cell_m)),
        ("units_ascii_out_resolution", "m"),
        ("write_goog_output", "false"),
        ("write_shapefile_output", "false"),
        ("write_farsite_atm", "false"),
        ("write_pdf_output", "false"),
        ("write_vtk_output", "false"),
        ("write_geotiff_output", "false"),
        ("output_path", out_dir),
    ]
    if momentum:
        pairs.extend(
            [
                ("momentum_flag", "true"),
                ("number_of_iterations", str(MOMENTUM_ITERATIONS)),
            ]
        )
    return pairs


def base_cfg(dem, out_dir, options=None, cell_m=CELL_M):
    if options is None:
        options = windninja_run_options_from_env()
    return build_base_cfg_pairs(dem, out_dir, options, cell_m=cell_m)


def ensure_momentum_supported(env):
    binary = shutil.which("WindNinja_cli", path=env["PATH"])
    if not binary:
        raise RuntimeError("WindNinja_cli is not installed")
    completed = subprocess.run(
        [binary, "--help"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    help_text = f"{completed.stdout}\n{completed.stderr}"
    if "momentum_flag" not in help_text:
        raise RuntimeError(
            "TERRAIN_WIND_MOMENTUM=true but this WindNinja build does not expose momentum_flag"
        )


def object_prefix_for_area(area_key, output_prefix):
    return f"{output_prefix}/{area_key}/"


def write_cfg(path, pairs):
    path.write_text("".join(f"{key} = {value}\n" for key, value in pairs), encoding="utf-8")


def run_windninja(cfg, env):
    binary = shutil.which("WindNinja_cli", path=env["PATH"])
    if not binary:
        raise RuntimeError("WindNinja_cli is not installed")
    run([binary, str(cfg)], env=env, timeout=240)


def sanitize_resampled_values(values, nodata):
    cleaned = []
    for value in values:
        if not math.isfinite(value) or abs(value - nodata) < 0.1 or value < -9000:
            cleaned.append(None)
        else:
            rounded = round(value, 1)
            cleaned.append(0.0 if rounded == 0 else rounded)
    return cleaned


def hour_record_payload(
    area_key,
    hour_stamp,
    bbox,
    nx,
    ny,
    cell_m,
    u_values,
    v_values,
    run_stamp=None,
    weather=None,
):
    record = {
        "ver": 1,
        "area": area_key,
        "run": run_stamp,
        "hour": hour_stamp,
        "bbox": bbox,
        "nx": nx,
        "ny": ny,
        "cell_m": cell_m,
        "u": u_values,
        "v": v_values,
    }
    if weather is not None:
        record["weather"] = weather
    return record


def output_grid(asc_path, bbox, nx, ny, work, name):
    tagged = work / f"{name}-4326.tif"
    warped = work / f"{name}-out.tif"
    asc = work / f"{name}-out.asc"
    run(["gdal_translate", "-q", "-a_srs", "EPSG:4326", str(asc_path), str(tagged)])
    run(
        [
            "gdalwarp",
            "-q",
            "-overwrite",
            "-t_srs",
            "EPSG:4326",
            "-te",
            str(bbox[0]),
            str(bbox[1]),
            str(bbox[2]),
            str(bbox[3]),
            "-ts",
            str(nx),
            str(ny),
            "-r",
            "bilinear",
            "-dstnodata",
            "-9999",
            str(tagged),
            str(warped),
        ]
    )
    run(["gdal_translate", "-q", "-of", "AAIGrid", str(warped), str(asc)])
    _ncols, _nrows, nodata, values = read_asc(asc)
    cleaned = sanitize_resampled_values(values, nodata)
    if len(cleaned) != nx * ny:
        raise RuntimeError("resampled grid does not match nx*ny")
    return cleaned


def grid_shape(bbox, cell_m=CELL_M):
    if cell_m not in ALLOWED_CELL_M:
        cell_m = CELL_M
    width_km, height_km = span_km(bbox)
    nx = max(2, int(round(width_km * 1000.0 / cell_m)))
    ny = max(2, int(round(height_km * 1000.0 / cell_m)))
    resolved = round(((width_km * 1000.0 / nx) + (height_km * 1000.0 / ny)) / 2.0, 1)
    return nx, ny, resolved


def find_uv(out_dir):
    u_files = sorted(out_dir.glob("*_u-4326.asc"))
    v_files = sorted(out_dir.glob("*_v-4326.asc"))
    if len(u_files) != 1 or len(v_files) != 1:
        raise RuntimeError("WindNinja did not write geographic u/v grids")
    return u_files[0], v_files[0]


def solve_hour(mode, grib, dem, bbox, valid, work, env, run_options, cell_m=CELL_M):
    out_dir = work / "out"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    pairs = base_cfg(dem, out_dir, run_options, cell_m=cell_m)
    if mode == "wx":
        pairs[2:2] = [
            ("initialization_method", "wxModelInitialization"),
            ("forecast_filename", grib),
        ]
    else:
        speed_path, dir_path, _mean_speed = build_speed_dir(grib, dem, bbox, work)
        temp, cloud = surface_means(grib, bbox, work)
        pairs[2:2] = [
            ("initialization_method", "griddedInitialization"),
            ("input_speed_grid", speed_path),
            ("input_dir_grid", dir_path),
            ("input_speed_units", "mps"),
            ("input_wind_height", "10"),
            ("units_input_wind_height", "m"),
            ("uni_air_temp", f"{temp:.2f}"),
            ("air_temp_units", "C"),
            ("uni_cloud_cover", f"{cloud:.1f}"),
            ("cloud_cover_units", "percent"),
            ("year", str(valid.year)),
            ("month", str(valid.month)),
            ("day", str(valid.day)),
            ("hour", str(valid.hour)),
            ("minute", "0"),
        ]
    cfg = work / "run.cfg"
    write_cfg(cfg, pairs)
    run_windninja(cfg, env)
    return find_uv(out_dir)


def scrub(message, bbox):
    text = str(message)
    for value in bbox:
        text = text.replace(str(value), "[redacted]")
        text = text.replace(f"{value:.6f}", "[redacted]")
        text = text.replace(f"{value:.4f}", "[redacted]")
        text = text.replace(f"{value:.2f}", "[redacted]")
    return text


def iso(when):
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def r2_env():
    missing = [
        name
        for name in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")
        if not os.environ.get(name, "").strip()
    ]
    if missing:
        raise RuntimeError("object storage is not configured")
    env = os.environ.copy()
    env["AWS_ACCESS_KEY_ID"] = os.environ["R2_ACCESS_KEY_ID"]
    env["AWS_SECRET_ACCESS_KEY"] = os.environ["R2_SECRET_ACCESS_KEY"]
    env["AWS_DEFAULT_REGION"] = "auto"
    endpoint = f"https://{os.environ['R2_ACCOUNT_ID'].strip()}.r2.cloudflarestorage.com"
    bucket = os.environ["R2_BUCKET"].strip()
    return env, endpoint, bucket


def aws(args, env):
    run(["aws", *args], env=env, timeout=180)


def upload_file(
    local,
    key,
    env,
    endpoint,
    bucket,
    gzip_encoding,
    content_type="application/json",
    cache_control=None,
):
    command = [
        "s3",
        "cp",
        str(local),
        f"s3://{bucket}/{key}",
        "--endpoint-url",
        endpoint,
        "--region",
        "auto",
        "--content-type",
        content_type,
        "--only-show-errors",
    ]
    if gzip_encoding:
        command.extend(["--content-encoding", "gzip"])
    if cache_control:
        command.extend(["--cache-control", cache_control])
    aws(command, env)
    return local.stat().st_size


def delete_old_runs(area_key, run_stamp, env, endpoint, bucket, output_prefix="wind"):
    prefix = object_prefix_for_area(area_key, output_prefix)
    token = None
    prefixes = []
    while True:
        command = [
            "s3api",
            "list-objects-v2",
            "--bucket",
            bucket,
            "--prefix",
            prefix,
            "--delimiter",
            "/",
            "--endpoint-url",
            endpoint,
            "--region",
            "auto",
            "--output",
            "json",
        ]
        if token:
            command.extend(["--continuation-token", token])
        completed = subprocess.run(
            ["aws", *command],
            check=True,
            capture_output=True,
            text=True,
            env=env,
            timeout=180,
        )
        payload = json.loads(completed.stdout or "{}")
        for item in payload.get("CommonPrefixes") or []:
            prefixes.append(item["Prefix"])
        if not payload.get("IsTruncated"):
            break
        token = payload.get("NextContinuationToken")
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    for folder in prefixes:
        name = folder[len(prefix) :].strip("/")
        if name == run_stamp:
            continue
        try:
            stamp = datetime.strptime(name, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if stamp >= cutoff:
            continue
        aws(
            [
                "s3",
                "rm",
                f"s3://{bucket}/{folder}",
                "--recursive",
                "--endpoint-url",
                endpoint,
                "--region",
                "auto",
                "--only-show-errors",
            ],
            env,
        )


def publish_area(area, records, run_stamp, env, endpoint, bucket, output_prefix="wind"):
    key = area["key"]
    total = 0
    hours = []
    for hour_stamp, payload in records:
        body = gzip.compress(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        local = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / f"{key}-{hour_stamp}.json.gz"
        local.write_bytes(body)
        object_key = f"{output_prefix}/{key}/{run_stamp}/{hour_stamp}.json.gz"
        total += upload_file(local, object_key, env, endpoint, bucket, True, cache_control=CACHE_IMMUTABLE)
        local.unlink(missing_ok=True)
        hour_index = {"hour": hour_stamp, "file": object_key}
        weather = payload.get("weather")
        if weather is not None:
            hour_index["weather"] = weather
        hours.append(hour_index)
    index = {"v": 1, "area": key, "run": run_stamp, "hours": hours}
    index_key = f"{output_prefix}/{key}/{run_stamp}/index.json"
    index_path = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / f"{key}-index.json"
    index_path.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    total += upload_file(index_path, index_key, env, endpoint, bucket, False, cache_control=CACHE_IMMUTABLE)
    latest = {"v": 1, "area": key, "run": run_stamp, "index": index_key}
    latest_path = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / f"{key}-latest.json"
    latest_path.write_text(json.dumps(latest, indent=2) + "\n", encoding="utf-8")
    total += upload_file(
        latest_path, f"{output_prefix}/{key}/latest.json", env, endpoint, bucket, False, cache_control=CACHE_LATEST
    )
    index_path.unlink(missing_ok=True)
    latest_path.unlink(missing_ok=True)
    if output_prefix == "wind":
        delete_old_runs(key, run_stamp, env, endpoint, bucket, output_prefix)
    covered = {"area": key, "run": run_stamp, "hours": [{"hour": h["hour"], "file": h["file"]} for h in hours]}
    return total, covered


def coverage_key(output_prefix="wind"):
    return f"{output_prefix}/coverage.json"


def merge_coverage(existing, planned, published):
    """This run's published tiles, plus the last entry for planned tiles that did not publish."""
    wanted = set(planned)
    tiles = {}
    previous = existing.get("tiles") if isinstance(existing, dict) else None
    for entry in list(previous or []) + list(published):
        if isinstance(entry, dict) and entry.get("area") in wanted:
            tiles[entry["area"]] = entry
    return {"v": 1, "tiles": [tiles[key] for key in sorted(tiles)]}


def hrrr_cache_dir_from_env():
    raw = os.environ.get("HRRR_CACHE_DIR", "").strip()
    return Path(raw) if raw else None


def process_area(area, cycle, prefix, cache_dir, storage, run_options):
    key = area["key"]
    bbox = area["bbox"]
    cell_m = area.get("cell_m", CELL_M)
    work_root = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "terrain-wind" / key
    if work_root.exists():
        shutil.rmtree(work_root)
    work_root.mkdir(parents=True)
    dem = dem_for(area, cache_dir)
    env = windninja_env(prefix)
    nx, ny, resolved_cell_m = grid_shape(bbox, cell_m=cell_m)
    mode = "wx"
    records = []
    utc_hours = run_options["utc_hours"]
    hrrr_bytes = 0
    for forecast_hour in FORECAST_HOURS:
        hour_work = work_root / f"f{forecast_hour:02d}"
        hour_work.mkdir()
        try:
            grib = hour_work / "hrrr.grib2"
            valid, subset_bytes = fetch_hrrr_subset_cached(
                cycle, forecast_hour, grib, hrrr_cache_dir_from_env()
            )
            if utc_hours is not None and valid.hour not in utc_hours:
                continue
            hrrr_bytes += subset_bytes
            weather = weather_from_grib(grib, bbox, hour_work)
            try:
                u_asc, v_asc = solve_hour(
                    mode, grib, dem, bbox, valid, hour_work, env, run_options, cell_m=cell_m
                )
            except Exception:
                if mode == "wx":
                    log(f"area {key} HRRR subset not accepted; using gridded initialization")
                    mode = "gridded"
                    u_asc, v_asc = solve_hour(
                        mode, grib, dem, bbox, valid, hour_work, env, run_options, cell_m=cell_m
                    )
                else:
                    raise
            u_values = output_grid(u_asc, bbox, nx, ny, hour_work, "u")
            v_values = output_grid(v_asc, bbox, nx, ny, hour_work, "v")
            hour_stamp = iso(valid)
            records.append(
                (
                    hour_stamp,
                    hour_record_payload(
                        key,
                        hour_stamp,
                        bbox,
                        nx,
                        ny,
                        resolved_cell_m,
                        u_values,
                        v_values,
                        weather=weather,
                    ),
                )
            )
        except Exception as exc:
            log(f"area {key} hour f{forecast_hour:02d} failed: {scrub(exc, bbox)}")
        finally:
            shutil.rmtree(hour_work, ignore_errors=True)
    if not records:
        raise RuntimeError("no hours produced")
    run_stamp = iso(datetime.now(timezone.utc).replace(microsecond=0))
    finished = []
    for hour_stamp, payload in records:
        payload["run"] = run_stamp
        finished.append((hour_stamp, payload))
    written, covered = publish_area(area, finished, run_stamp, *storage, run_options["output_prefix"])
    per_hour = hrrr_bytes / len(finished) if finished else 0
    log(
        f"area {key} hours={len(finished)} bytes={written} mode={mode} "
        f"hrrr_bytes={hrrr_bytes} hrrr_bytes_per_hour={per_hour:.0f}"
    )
    return written, covered


def execute():
    script_dir = Path(__file__).resolve().parent
    if str(script_dir) not in sys.path:
        sys.path.insert(0, str(script_dir))
    from run_status import RunStatusReporter

    status = RunStatusReporter("terrain-wind")
    status.start()
    started = datetime.now(timezone.utc)
    conclusion = "success"
    exit_code = 0
    status_fields = {
        "tiles_requested": 0,
        "tiles_ok": [],
        "tiles_failed": [],
        "tiles_capped": [],
        "areas_oversized": 0,
        "areas_coarse": 0,
        "hrrr_cycle": None,
    }
    uploaded = 0
    failed = 0
    try:
        areas_path = Path(os.environ.get("AREAS_FILE", "areas.json"))
        if not areas_path.is_file():
            log("no areas source")
            return
        document = json.loads(areas_path.read_text(encoding="utf-8"))
        wind_meta = document.get("wind_meta") if isinstance(document, dict) else None
        if not isinstance(wind_meta, dict):
            wind_meta = {}
        status_fields["areas_oversized"] = int(wind_meta.get("areas_oversized") or 0)
        status_fields["areas_coarse"] = int(wind_meta.get("areas_coarse") or 0)
        capped = wind_meta.get("tiles_capped")
        status_fields["tiles_capped"] = list(capped) if isinstance(capped, list) else []
        areas = validate_areas(document)
        areas = filter_areas_by_tiles(areas, tile_filter_from_env())
        status_fields["tiles_requested"] = len(areas)
        if not areas:
            log("no areas source")
            return
        storage = r2_env()
        run_options = windninja_run_options_from_env()
        prefix = Path(os.environ.get("WINDNINJA_PREFIX", str(Path.home() / "windninja")))
        wn_env = windninja_env(prefix)
        if run_options["momentum"]:
            ensure_momentum_supported(wn_env)
        cache_dir = Path(os.environ.get("DEM_CACHE", "dem-cache"))
        cycle = resolve_hrrr_cycle()
        status_fields["hrrr_cycle"] = iso(cycle)
        log(f"hrrr_cycle={iso(cycle)}")
        if run_options["output_prefix"] != "wind":
            log(f"output_prefix={run_options['output_prefix']}")
        tiles_ok = []
        tiles_failed = []
        published = []
        for area in areas:
            try:
                written, covered = process_area(area, cycle, prefix, cache_dir, storage, run_options)
                uploaded += written
                published.append(covered)
                tiles_ok.append(area["key"])
            except Exception as exc:
                failed += 1
                tiles_failed.append(area["key"])
                log(f"area {area['key']} failed: {scrub(exc, area['bbox'])}")
        status_fields["tiles_ok"] = tiles_ok
        status_fields["tiles_failed"] = tiles_failed
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        write_result_file(tiles_ok, tiles_failed, uploaded, elapsed, published)
        log(
            f"runtime_s={elapsed:.0f} uploaded_bytes={uploaded} "
            f"uploaded_mb={uploaded / 1_000_000:.3f} areas_failed={failed}"
        )
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as handle:
                handle.write(
                    f"runtime_s={elapsed:.0f} uploaded_mb={uploaded / 1_000_000:.3f} "
                    f"areas_failed={failed}\n"
                )
        if failed:
            exit_code = 1
            conclusion = "partial" if tiles_ok else "failure"
    finally:
        status.finish(conclusion, **status_fields)
    if exit_code:
        sys.exit(exit_code)


BATCH_SIZE = 2


def batch_areas(areas, size=BATCH_SIZE):
    ordered = sorted(areas, key=lambda area: area["key"])
    return [ordered[start : start + size] for start in range(0, len(ordered), size)]


def batch_cache_key(areas):
    stamp = json.dumps(
        sorted([area["key"], area["bbox"], area.get("cell_m", CELL_M)] for area in areas),
        separators=(",", ":"),
    )
    return hashlib.sha256(stamp.encode()).hexdigest()[:16]


def write_result_file(tiles_ok, tiles_failed, uploaded_bytes, runtime_s, published=()):
    raw = os.environ.get("RUN_RESULT_FILE", "").strip()
    if not raw:
        return
    path = Path(raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "tiles_ok": tiles_ok,
                "tiles_failed": tiles_failed,
                "uploaded_bytes": uploaded_bytes,
                "runtime_s": round(runtime_s, 1),
                "published": list(published),
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )


def import_run_status():
    script_dir = Path(__file__).resolve().parent
    if str(script_dir) not in sys.path:
        sys.path.insert(0, str(script_dir))
    import run_status

    return run_status


def plan():
    """Split resolved areas into batches, write the start status, pin the HRRR cycle."""
    document = json.loads(Path(os.environ.get("AREAS_FILE", "areas.json")).read_text(encoding="utf-8"))
    areas = validate_areas(document)
    wind_meta = document.get("wind_meta") if isinstance(document, dict) else None
    if not isinstance(wind_meta, dict):
        wind_meta = {}
    capped = wind_meta.get("tiles_capped")
    meta = {
        "areas_oversized": int(wind_meta.get("areas_oversized") or 0),
        "areas_coarse": int(wind_meta.get("areas_coarse") or 0),
        "tiles_capped": list(capped) if isinstance(capped, list) else [],
    }
    batches = [
        {"batch": index, "keys": [area["key"] for area in group]}
        for index, group in enumerate(batch_areas(areas))
    ]
    emit("batches", json.dumps(batches, separators=(",", ":")))
    emit("batch_indexes", json.dumps([item["batch"] for item in batches], separators=(",", ":")))
    emit("meta", json.dumps(meta, separators=(",", ":")))
    log(f"tiles={len(areas)} batches={len(batches)}")
    if not batches:
        return
    status = import_run_status().RunStatusReporter("terrain-wind")
    emit("started", status.started_wall)
    status.start()
    cycle = resolve_hrrr_cycle()
    emit("hrrr_cycle", iso(cycle))
    log(f"hrrr_cycle={iso(cycle)}")


def batch():
    """Write this job's batch (BATCH_INDEX) to BATCH_FILE and emit its DEM cache key."""
    document = json.loads(Path(os.environ.get("AREAS_FILE", "areas.json")).read_text(encoding="utf-8"))
    groups = batch_areas(validate_areas(document))
    index = int(os.environ["BATCH_INDEX"])
    if index < 0 or index >= len(groups):
        log("batch index out of range")
        sys.exit(1)
    out_path = Path(os.environ.get("BATCH_FILE", "batch.json"))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"areas": groups[index]}), encoding="utf-8")
    emit("cache_key", batch_cache_key(groups[index]))
    log(f"batch={index} tiles={len(groups[index])}")


def merge_batch_results(planned_keys, results, plan_ok=True):
    """Fold per-batch result files into the run-level status fields.

    A planned tile with no ok report (crashed or missing batch) counts as failed.
    """
    planned = list(planned_keys)
    wanted = set(planned)
    ok_seen = set()
    uploaded = 0
    runtime = 0.0
    for result in results if plan_ok else []:
        ok_seen.update(key for key in result.get("tiles_ok", []) if key in wanted)
        uploaded += int(result.get("uploaded_bytes") or 0)
        runtime = max(runtime, float(result.get("runtime_s") or 0))
    tiles_ok = [key for key in planned if key in ok_seen]
    tiles_failed = [key for key in planned if key not in ok_seen]
    if tiles_failed:
        conclusion = "partial" if tiles_ok else "failure"
    else:
        conclusion = "success"
    return {
        "conclusion": conclusion,
        "tiles_ok": tiles_ok,
        "tiles_failed": tiles_failed,
        "uploaded_bytes": uploaded,
        "max_batch_runtime_s": runtime,
    }


def finalize():
    """Merge batch results and write the finish status once."""
    run_status = import_run_status()
    try:
        batches = json.loads(os.environ.get("PLAN_BATCHES") or "[]")
        meta = json.loads(os.environ.get("PLAN_META") or "{}")
    except json.JSONDecodeError:
        batches, meta = [], {}
    planned = [key for item in batches for key in item.get("keys", [])]
    results = []
    results_dir = Path(os.environ.get("RESULTS_DIR", "results"))
    if results_dir.is_dir():
        for path in sorted(results_dir.glob("*.json")):
            try:
                results.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                log(f"ignoring unreadable result {path.name}")
    plan_ok = os.environ.get("PLAN_RESULT", "success") == "success"
    merged = merge_batch_results(planned, results, plan_ok=plan_ok)
    status = run_status.RunStatusReporter("terrain-wind")
    capped = meta.get("tiles_capped")
    status.finish(
        merged["conclusion"],
        tiles_requested=len(planned),
        tiles_ok=merged["tiles_ok"],
        tiles_failed=merged["tiles_failed"],
        tiles_capped=list(capped) if isinstance(capped, list) else [],
        areas_oversized=int(meta.get("areas_oversized") or 0),
        areas_coarse=int(meta.get("areas_coarse") or 0),
        hrrr_cycle=os.environ.get("HRRR_CYCLE", "").strip() or None,
    )
    try:
        key = coverage_key(normalize_output_prefix(os.environ.get("TERRAIN_WIND_PREFIX", "wind")))
        published = [entry for result in (results if plan_ok else []) for entry in result.get("published") or []]
        coverage = merge_coverage(status.store.read_json(key), planned, published)
        status.store.write_json(key, coverage, cache_control=CACHE_LATEST)
    except Exception as exc:
        log(f"coverage write failed: {type(exc).__name__}")
    line = (
        f"conclusion={merged['conclusion']} tiles_ok={len(merged['tiles_ok'])} "
        f"tiles_failed={len(merged['tiles_failed'])} uploaded_bytes={merged['uploaded_bytes']} "
        f"batches_reported={len(results)}"
    )
    log(line)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    if merged["tiles_failed"]:
        sys.exit(1)


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else "run"
    if command == "resolve":
        resolve()
    elif command == "run":
        execute()
    elif command == "plan":
        plan()
    elif command == "batch":
        batch()
    elif command == "finalize":
        finalize()
    else:
        log("unknown command")
        sys.exit(2)


if __name__ == "__main__":
    main()

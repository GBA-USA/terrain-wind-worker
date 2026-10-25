#!/usr/bin/env python3
"""Fetch areas, run WindNinja, and publish hourly u/v grids to R2.

Areas are runtime input only. This script does not print secrets or bboxes.
"""

import gzip
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
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
MAX_EDGE_KM = 40
TILE_EPS = 1e-9
TILE_CAP = 60
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


def validate_areas(document):
    if not isinstance(document, dict) or not isinstance(document.get("areas"), list):
        raise RuntimeError("areas response was not usable")
    areas = []
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
        width_km, height_km = span_km((min_lon, min_lat, max_lon, max_lat))
        if width_km > MAX_EDGE_KM or height_km > MAX_EDGE_KM:
            log(f"area {key} skipped: domain exceeds {MAX_EDGE_KM} km")
            continue
        areas.append({"key": key, "bbox": [min_lon, min_lat, max_lon, max_lat]})
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


def expand_areas_to_tiles(areas):
    tiles = {}
    for area in areas:
        tiles.update(tiles_for_bbox(area["bbox"]))
    expanded = areas_from_tiles(tiles)
    if len(expanded) > TILE_CAP:
        skipped = len(expanded) - TILE_CAP
        log(f"tile cap reached, skipped {skipped}")
        expanded = expanded[:TILE_CAP]
    return expanded


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
        areas = validate_areas(document)
    except RuntimeError as exc:
        log(str(exc))
        sys.exit(1)
    if not test_mode:
        areas = expand_areas_to_tiles(areas)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"areas": areas}), encoding="utf-8")
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
    query = urllib.parse.urlencode(
        {
            "bbox": f"{min_lon},{min_lat},{max_lon},{max_lat}",
            "bboxSR": "4326",
            "imageSR": "4326",
            "size": f"{width},{height}",
            "format": "tiff",
            "pixelType": "F32",
            "interpolation": "RSP_BilinearInterpolation",
            "f": "image",
        }
    )
    try:
        data, _headers = http_bytes(f"{DEM_URL}?{query}", timeout=180)
    except Exception:
        raise RuntimeError("DEM download failed") from None
    if data[:4] not in (b"II*\x00", b"MM\x00*"):
        raise RuntimeError("DEM download failed")
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


def hrrr_object(cycle, forecast_hour):
    ymd = cycle.strftime("%Y%m%d")
    hour = cycle.strftime("%H")
    return (
        f"{HRRR_BASE}/hrrr.{ymd}/conus/"
        f"hrrr.t{hour}z.wrfsfcf{forecast_hour:02d}.grib2"
    )


def fetch_hrrr_subset(cycle, forecast_hour, dest):
    url = hrrr_object(cycle, forecast_hour)
    try:
        index_text = http_bytes(url + ".idx", timeout=60)[0].decode("utf-8", "replace")
        size = content_length(url)
    except Exception:
        raise RuntimeError("HRRR download failed") from None
    rows = []
    for line in index_text.splitlines():
        parts = line.split(":")
        if len(parts) < 6:
            continue
        try:
            start = int(parts[1])
        except ValueError:
            continue
        rows.append((start, parts[3], parts[4]))
    wanted = []
    for index, (start, variable, level) in enumerate(rows):
        take = (
            (variable == "TMP" and level.startswith("2 m above ground"))
            or (variable in ("UGRD", "VGRD") and level.startswith("10 m above ground"))
            or (variable == "TCDC" and level.startswith("entire atmosphere"))
        )
        if not take:
            continue
        end = rows[index + 1][0] - 1 if index + 1 < len(rows) else size - 1
        wanted.append((variable, start, end))
    names = {item[0] for item in wanted}
    if names != {"TMP", "UGRD", "VGRD", "TCDC"}:
        raise RuntimeError("HRRR subset is missing 10 m wind, 2 m temperature, or cloud cover")
    order = {"TMP": 0, "UGRD": 1, "VGRD": 2, "TCDC": 3}
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
    return cycle + timedelta(hours=forecast_hour)


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


def surface_means(grib, bbox, work):
    info = gdal_info(grib)
    temp_band = band_number(info, "Temperature", "2-HTGL")
    cloud_band = band_number(info, "Total cloud cover", "0-EATM")
    temp = sample_band_mean(grib, temp_band, bbox, work)
    cloud = sample_band_mean(grib, cloud_band, bbox, work)
    if temp > 150.0:
        temp -= 273.15
    if cloud <= 1.5:
        cloud *= 100.0
    return temp, max(0.0, min(100.0, cloud))


def windninja_env(prefix):
    env = os.environ.copy()
    env["WINDNINJA_DATA"] = str(prefix / "share" / "windninja")
    env["PATH"] = str(prefix / "bin") + os.pathsep + env.get("PATH", "")
    return env


def base_cfg(dem, out_dir):
    return [
        ("num_threads", "2"),
        ("elevation_file", dem),
        ("time_zone", "UTC"),
        ("output_wind_height", "10"),
        ("units_output_wind_height", "m"),
        ("output_speed_units", "mps"),
        ("vegetation", "grass"),
        ("mesh_resolution", str(CELL_M)),
        ("units_mesh_resolution", "m"),
        ("diurnal_winds", "true"),
        ("non_neutral_stability", "true"),
        ("write_ascii_output", "true"),
        ("ascii_out_aaigrid", "true"),
        ("ascii_out_json", "false"),
        ("ascii_out_proj", "false"),
        ("ascii_out_geog", "true"),
        ("ascii_out_uv", "true"),
        ("ascii_out_resolution", str(CELL_M)),
        ("units_ascii_out_resolution", "m"),
        ("write_goog_output", "false"),
        ("write_shapefile_output", "false"),
        ("write_farsite_atm", "false"),
        ("write_pdf_output", "false"),
        ("write_vtk_output", "false"),
        ("write_geotiff_output", "false"),
        ("output_path", out_dir),
    ]


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


def hour_record_payload(area_key, hour_stamp, bbox, nx, ny, cell_m, u_values, v_values, run_stamp=None):
    return {
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


def grid_shape(bbox):
    width_km, height_km = span_km(bbox)
    nx = max(2, int(round(width_km * 1000.0 / CELL_M)))
    ny = max(2, int(round(height_km * 1000.0 / CELL_M)))
    cell_m = round(((width_km * 1000.0 / nx) + (height_km * 1000.0 / ny)) / 2.0, 1)
    return nx, ny, cell_m


def find_uv(out_dir):
    u_files = sorted(out_dir.glob("*_u-4326.asc"))
    v_files = sorted(out_dir.glob("*_v-4326.asc"))
    if len(u_files) != 1 or len(v_files) != 1:
        raise RuntimeError("WindNinja did not write geographic u/v grids")
    return u_files[0], v_files[0]


def solve_hour(mode, grib, dem, bbox, valid, work, env):
    out_dir = work / "out"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    pairs = base_cfg(dem, out_dir)
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


def upload_file(local, key, env, endpoint, bucket, gzip_encoding):
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
        "application/json",
        "--only-show-errors",
    ]
    if gzip_encoding:
        command.extend(["--content-encoding", "gzip"])
    aws(command, env)
    return local.stat().st_size


def delete_old_runs(area_key, run_stamp, env, endpoint, bucket):
    prefix = f"wind/{area_key}/"
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


def publish_area(area, records, run_stamp, env, endpoint, bucket):
    key = area["key"]
    total = 0
    hours = []
    for hour_stamp, payload in records:
        body = gzip.compress(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        local = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / f"{key}-{hour_stamp}.json.gz"
        local.write_bytes(body)
        object_key = f"wind/{key}/{run_stamp}/{hour_stamp}.json.gz"
        total += upload_file(local, object_key, env, endpoint, bucket, True)
        local.unlink(missing_ok=True)
        hours.append({"hour": hour_stamp, "file": object_key})
    index = {"v": 1, "area": key, "run": run_stamp, "hours": hours}
    index_key = f"wind/{key}/{run_stamp}/index.json"
    index_path = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / f"{key}-index.json"
    index_path.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    total += upload_file(index_path, index_key, env, endpoint, bucket, False)
    latest = {"v": 1, "area": key, "run": run_stamp, "index": index_key}
    latest_path = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / f"{key}-latest.json"
    latest_path.write_text(json.dumps(latest, indent=2) + "\n", encoding="utf-8")
    total += upload_file(latest_path, f"wind/{key}/latest.json", env, endpoint, bucket, False)
    index_path.unlink(missing_ok=True)
    latest_path.unlink(missing_ok=True)
    delete_old_runs(key, run_stamp, env, endpoint, bucket)
    return total


def process_area(area, cycle, prefix, cache_dir, storage):
    key = area["key"]
    bbox = area["bbox"]
    work_root = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "terrain-wind" / key
    if work_root.exists():
        shutil.rmtree(work_root)
    work_root.mkdir(parents=True)
    dem = dem_for(area, cache_dir)
    env = windninja_env(prefix)
    nx, ny, cell_m = grid_shape(bbox)
    mode = "wx"
    records = []
    for forecast_hour in FORECAST_HOURS:
        hour_work = work_root / f"f{forecast_hour:02d}"
        hour_work.mkdir()
        try:
            grib = hour_work / "hrrr.grib2"
            valid = fetch_hrrr_subset(cycle, forecast_hour, grib)
            try:
                u_asc, v_asc = solve_hour(mode, grib, dem, bbox, valid, hour_work, env)
            except Exception:
                if mode == "wx":
                    log(f"area {key} HRRR subset not accepted; using gridded initialization")
                    mode = "gridded"
                    u_asc, v_asc = solve_hour(mode, grib, dem, bbox, valid, hour_work, env)
                else:
                    raise
            u_values = output_grid(u_asc, bbox, nx, ny, hour_work, "u")
            v_values = output_grid(v_asc, bbox, nx, ny, hour_work, "v")
            hour_stamp = iso(valid)
            records.append(
                (
                    hour_stamp,
                    hour_record_payload(key, hour_stamp, bbox, nx, ny, cell_m, u_values, v_values),
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
    written = publish_area(area, finished, run_stamp, *storage)
    log(f"area {key} hours={len(finished)} bytes={written} mode={mode}")
    return written


def execute():
    started = datetime.now(timezone.utc)
    areas_path = Path(os.environ.get("AREAS_FILE", "areas.json"))
    if not areas_path.is_file():
        log("no areas source")
        return
    areas = validate_areas(json.loads(areas_path.read_text(encoding="utf-8")))
    if not areas:
        log("no areas source")
        return
    storage = r2_env()
    prefix = Path(os.environ.get("WINDNINJA_PREFIX", str(Path.home() / "windninja")))
    cache_dir = Path(os.environ.get("DEM_CACHE", "dem-cache"))
    cycle = latest_hrrr_cycle()
    log(f"hrrr_cycle={iso(cycle)}")
    failed = 0
    uploaded = 0
    for area in areas:
        try:
            uploaded += process_area(area, cycle, prefix, cache_dir, storage)
        except Exception as exc:
            failed += 1
            log(f"area {area['key']} failed: {scrub(exc, area['bbox'])}")
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
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
        sys.exit(1)


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

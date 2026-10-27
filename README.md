# terrain-wind-worker

Scheduled [WindNinja](https://github.com/firelab/windninja) 4.0.0 runs on GitHub Actions. The mass-conserving solver (CLI, cached on the runner) writes hourly terrain-wind grids to Cloudflare R2. Area definitions are fetched at run time and are not stored here.

## When it runs

- Every 3 hours
- `workflow_dispatch` with `test=true` uses `fixtures/test-area.json` and does not call `AREAS_URL`
- `repository_dispatch` type `areas-changed`

One run at a time (`terrain-wind`). A failed area does not stop the others. Domains wider than 40 km are skipped.

## Secrets

`R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`, `AREAS_URL`, `AREAS_TOKEN`.

S3 endpoint `https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com`, region `auto`. If `AREAS_URL` or `AREAS_TOKEN` is unset and this is not a fixture run, the job logs `no areas source` and exits successfully.

## AREAS_URL

`GET` with header `Authorization: Bearer <AREAS_TOKEN>`.

```json
{"areas":[{"key":"<opaque string>","bbox":[minLon,minLat,maxLon,maxLat]}]}
```

`bbox` is west, south, east, north in WGS84 degrees.

## Objects

- `wind/<key>/<runISO>/<hourISO>.json.gz` — `Content-Type: application/json`, `Content-Encoding: gzip`
- `wind/<key>/<runISO>/index.json` — hours in that run
- `wind/<key>/latest.json` — `{"v":1,"area","run","index"}` pointing at the newest index

Hour JSON:

```json
{"ver":1,"area":"<key>","run":"<ISO-8601>","hour":"<ISO-8601>","bbox":[minLon,minLat,maxLon,maxLat],"nx":0,"ny":0,"cell_m":100,"u":[],"v":[],"weather":{"cloud_pct":0,"cloud_low_pct":0,"relative_humidity_pct":0,"dew_point_c":0,"temperature_c":0,"gust_mph":0,"precip_mm_per_h":0,"shortwave_wm2":0,"weather_code":0}}
```

`u` (east) and `v` (north) are m/s rounded to 0.1, row-major from the northwest corner; cells with no data are JSON `null`. Timestamps are UTC `YYYY-MM-DDTHH:MM:SSZ`. Runs older than 48 hours are deleted.

`weather` holds area-mean HRRR scalars for scent alerts (same keys as IversonAcres `scent-weather.server.ts`): `cloud_pct`, `cloud_low_pct` (LCDC), `relative_humidity_pct`, `dew_point_c`, `temperature_c`, `gust_mph`, `precip_mm_per_h`, `shortwave_wm2`, and WMO `weather_code`. Each `index.json` hour entry repeats the same object inline as `weather` (scalars are small; no `weather_file` sidecar).

Each hour uses NOAA HRRR 10 m wind plus those scalar fields (AWS Open Data GRIB `.idx` byte ranges), a USGS 3DEP DEM cached by area key, and WindNinja with diurnal winds and non-neutral stability.

## Offline packs

Separate workflow `offline-packs` builds public map chunks (Mapterhorn DEM + PAD-US) into the same R2 bucket. Triggers: daily `30 7 * * *` UTC, `workflow_dispatch` with `test=true` (fixture areas), and `repository_dispatch` type `areas-changed`. Concurrency group `offline-packs`.

Per z14 web-mercator chunk:

```
packs/v1/z14/{x}/{y}/manifest.json
packs/v1/z14/{x}/{y}/dem.pmtiles
packs/v1/z14/{x}/{y}/public-land.geojson   # gzip Content-Encoding on upload
```

Manifest example:

```json
{
  "v": 1,
  "chunk": [14, 0, 0],
  "bbox": [west, south, east, north],
  "built": "2026-10-26T08:00:00Z",
  "files": [
    {
      "name": "dem.pmtiles",
      "bytes": 0,
      "sha256": "…",
      "zooms": [12, 17],
      "tileUrl": "https://tiles.mapterhorn.com/{z}/{x}/{y}.webp"
    },
    {
      "name": "public-land.geojson",
      "bytes": 0,
      "sha256": "…",
      "features": 0
    }
  ],
  "bytes": 0,
  "attribution": ["Relief: Mapterhorn", "PAD-US: USGS"]
}
```

Chunks whose manifest is missing, older than 30 days, or not `v: 1` are rebuilt. Caps: 64 chunks per area and 400 per run.

## Contour tiles

Separate workflow `contour-tiles` prebuilds USGS TNM contour export PNGs for wind areas (and optional client miss backfill) into R2. Triggers: weekly Monday `09:00` UTC, `workflow_dispatch` with `test=true` (fixture areas), and `repository_dispatch` type `areas-changed`. Concurrency group `contour-tiles`.

Grid zoom **z = 11..15** (app map zoom 12..16 with `zoomOffset: -1`). Each object is a 512×512 PNG fetched from `carto.nationalmap.gov` using the same export query as the app (`contourTileUrl` in IversonAcres `src/lib/club/elevation.ts`), with `size=512,512`.

### R2 keys

```
tiles/contours/v1/512/{z}/{x}/{y}.png
```

`Content-Type: image/png`, `Cache-Control: public, max-age=2592000`. Existing objects younger than 90 days are skipped (`HEAD` on LastModified). Caps: 3,000 tiles per run, concurrency 4, TNM retries with backoff.

Optional `MISSES_URL` (same `AREAS_TOKEN` bearer): JSON `{"tiles":[[z,x,y],...]}`. When unset, the job logs `no misses source` and continues.

### App read path

Serve through a Cloudflare Worker route (Workers Cache API) or a custom domain on the bucket — not the rate-limited `pub-*.r2.dev` URL in production. Example path the app should request:

```
https://<your-worker-or-custom-domain>/tiles/contours/v1/512/{z}/{x}/{y}.png
```

The worker can origin-fetch from R2 using the same key layout above. Attribution: include USGS National Geospatial Program acknowledgment for contours.

Dry-run tile planning (no R2):

```bash
TEST_MODE=true AREAS_FILE=fixtures/test-area.json python3 scripts/contour_tiles.py plan
```

## License

MIT. WindNinja has its own license.

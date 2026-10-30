# terrain-wind-worker

Scheduled [WindNinja](https://github.com/firelab/windninja) 4.0.0 runs on GitHub Actions. The mass-conserving solver (CLI, cached on the runner) writes hourly terrain-wind grids to Cloudflare R2. Area definitions are fetched at run time and are not stored here.

## When it runs

- Every 3 hours, at minute 17 (`17 */3 * * *`; off the top of the hour, where GitHub delays and drops scheduled runs)
- `workflow_dispatch` with `test=true` uses `fixtures/test-area.json` and does not call `AREAS_URL`
- `repository_dispatch` type `areas-changed`

One run at a time (`terrain-wind`; a new run queues behind a running one). Each run is three jobs:

1. `plan` resolves areas, writes the start status once, and pins one HRRR cycle for the whole run. It splits the tiles (sorted by key) into batches of 2 and uploads the areas as an artifact, encrypted with `AREAS_TOKEN` because artifacts on a public repo are readable by anyone.
2. `wind` is a matrix with one job per batch (`max-parallel: 10`, `fail-fast: false`, 30 min timeout). Each job runs its tiles with the pinned cycle (`TERRAIN_WIND_HRRR_CYCLE`), downloads each HRRR forecast hour once and reuses it for both tiles, skips status writes (`RUN_STATUS_SKIP=true`), and uploads a small result artifact (`tiles_ok`, `tiles_failed`, `uploaded_bytes`, `runtime_s`). Each batch has its own DEM cache.
3. `finalize` merges the result artifacts and writes the finish status and history once. It fails if any tile failed, or if a batch produced no result.

A full run is about 8 batches in parallel for 15 tiles, so wall time is roughly one batch (~10 min) instead of the sum of all tiles. Output keys and payloads are unchanged. A failed area does not stop the others. Domains wider than 40 km are skipped at 100 m resolution; optional `cell_m: 200` allows edges up to 80 km before skip.

Failed scheduled or `repository_dispatch` runs are automatically retried once (`auto-retry` workflow reruns failed jobs when `run_attempt` is 1). That reruns only the failed batch jobs plus `finalize`, reusing the plan's HRRR cycle and the other batches' results. Manual and test dispatches are not retried.

## Run status (R2)

Each production run writes public status objects (`plan` writes the start, `finalize` writes the finish and history; batch jobs write none) under `status/<workflow>/` (`status-test/` for fixture runs or a non-`wind` output prefix):

- `latest.json` — current run: `{v, workflow, run_id, run_attempt, event, started, finished, timeout_s, conclusion, …counts}`
- `history.json` — last 40 runs (newest first), keyed by `run_id` + `run_attempt`

`Cache-Control: no-cache`. Timed-out or killed runs may leave `finished: null`. Counts and tile keys only (no club ids, bboxes, tokens, or URLs).

`terrain-wind` finish counts: `tiles_requested`, `tiles_ok`, `tiles_failed`, `tiles_capped`, `areas_oversized`, `areas_coarse`, `hrrr_cycle`.  
`offline-packs` finish counts: `chunks_built`, `chunks_skipped`, `chunks_failed`, `uploaded_mb`.

Workflow env `RUN_TIMEOUT_S` is 1800 seconds, matching the 30 minute per-batch `wind` job timeout. A batch that times out or crashes writes no result, so its tiles are reported in `tiles_failed`. `runtime_s` covers the whole run, from the plan job's start to finalize.

## Secrets

`R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`, `AREAS_URL`, `AREAS_TOKEN`.

S3 endpoint `https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com`, region `auto`. If `AREAS_URL` or `AREAS_TOKEN` is unset and this is not a fixture run, the job logs `no areas source` and exits successfully.

## AREAS_URL

`GET` with header `Authorization: Bearer <AREAS_TOKEN>`.

```json
{"areas":[{"key":"<opaque string>","bbox":[minLon,minLat,maxLon,maxLat],"cell_m":100}]}
```

Optional `cell_m` is `100` (default) or `200` for coarser grids on larger domains. Areas are split into 0.1° tiles; overlapping tiles use the smallest `cell_m`. A per-run cost budget (60 at 100 m, 0.5 per 200 m tile) caps work; dropped tile keys appear in status as `tiles_capped`.

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

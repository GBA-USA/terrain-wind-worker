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
{"v":1,"area":"<key>","run":"<ISO-8601>","hour":"<ISO-8601>","bbox":[minLon,minLat,maxLon,maxLat],"nx":0,"ny":0,"cell_m":100,"u":[],"v":[]}
```

`u` (east) and `v` (north) are m/s rounded to 0.1, row-major from the northwest corner. Timestamps are UTC `YYYY-MM-DDTHH:MM:SSZ`. Runs older than 48 hours are deleted.

Each hour uses NOAA HRRR 10 m wind (AWS Open Data byte ranges), a USGS 3DEP DEM cached by area key, and WindNinja with diurnal winds and non-neutral stability.

## License

MIT. WindNinja has its own license.

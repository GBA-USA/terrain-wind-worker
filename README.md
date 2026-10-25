# terrain-wind-worker

Computes terrain-adjusted wind grids with [WindNinja](https://github.com/firelab/windninja) on GitHub Actions and publishes the results to object storage.

- Runs on a schedule or when the requested area list changes (never on every push).
- Inputs (elevation and forecast data) are fetched at run time; no inputs or outputs are stored in this repository.
- Results are uploaded to an S3-compatible bucket configured through repository secrets.

## License

MIT. See the WindNinja project for its own license terms.

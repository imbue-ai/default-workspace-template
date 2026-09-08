# dashboards

A prototype of analyst-facing dashboarding on [Evidence](https://evidence.dev)
over the minds analytics data. The first page charts the OpenTelemetry host
metrics of the bare-metal slice boxes registered to one dev env, read straight
from the tier's OpenObserve telemetry parquet in R2 (the same source the
analytics aggregation's log views use).

This is deliberately a local, operator-run prototype: no deploy dependency,
nothing here ships anywhere. It follows the same pattern as `../reports/`
(analyst-owned SQL), with Evidence as the presentation layer.

## How it fits together

1. `extract_box_metrics.py` reads the env's locally persisted analytics
   credentials (`~/.minds-<env>/secrets.toml`, written by
   `minds-admin env deploy --with-analytics`): the read-only key on the tier's
   shared OpenObserve bucket plus the `analytics_reader` DSN on the env's
   connector database. It identifies the env's boxes from the
   `bare_metal_servers` table, scans the hostmetrics parquet streams for just
   those hosts, pre-aggregates them into small chart-ready tables, and writes
   `data/box_metrics.duckdb` (gitignored).
2. The Evidence `boxes` source (`sources/boxes/`) is a DuckDB connection to
   that file; each `.sql` file there becomes a queryable table.
3. `pages/index.md` is the dashboard: CPU, load, memory, per-slice qemu
   process metrics, filesystem, and network charts per box.

The metric streams live at `files/default/metrics/<stream>/` in the
observability bucket -- an OpenObserve-internal layout, pinned in the extract
script with the same re-verify-on-upgrade caveat as the analytics app's log
views. Box collectors and their scrapers are defined in
`apps/observability/imbue/observability/collector_install.py`.

## Running it

From the repo root (needs node >= 20 and an env deployed with analytics):

```bash
# 1. Extract the last week of box telemetry for the env.
uv run python apps/analytics/dashboards/extract_box_metrics.py --env dev-josh-1

# 2. Build the Evidence tables and start the dev server.
cd apps/analytics/dashboards
npm install
npm run sources
npm run dev
```

`npm run dev` serves the dashboard on http://localhost:3000 with hot reload
for page edits. Re-run the extract (then `npm run sources`) to refresh the
data. `npm run build` writes a static site to `build/`.

Note on formats: `evidence.config.yaml` and `sources/boxes/connection.yaml`
are YAML because Evidence mandates it (a third-party tool's format, like the
OpenTelemetry Collector's config); our own configuration stays TOML.

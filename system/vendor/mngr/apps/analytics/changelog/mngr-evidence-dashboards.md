Added `dashboards/`, a local Evidence.dev dashboarding prototype over the analytics data sources.

- `extract_box_metrics.py` pulls a dev env's bare-metal box OpenTelemetry host metrics (CPU, load, memory, filesystem, network, and per-slice qemu process metrics) from the tier's OpenObserve parquet in R2 -- scoped to the boxes in the env's `bare_metal_servers` table -- into a small local DuckDB file, using the env's locally persisted analytics credentials.

- An Evidence project (`pages/index.md` plus the `boxes` DuckDB source) charts those tables; run it with `npm run sources && npm run dev`. Operator-run and local-only, like `reports/`: no deploy dependency.

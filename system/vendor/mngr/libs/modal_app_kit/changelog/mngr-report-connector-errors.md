Extends the shared Bugsink reporting helper (whose bring-up this branch carries via a merge of `mngr/env-tier-sentry` until that lands on main):

- `init_sentry` now reports stdlib `logging` records at WARNING and above as events (previously ERROR+): warning is the lower-priority channel for exceptions the app caught and continued past, error the top-priority one.

- New `capture_unexpected_exception` helper: explicit exception capture returning the event id, for app-level 500 handlers that embed it in their response (the SDK's Dedupe integration prevents double-reporting).

- New `metrics.py` module: `emit_metric` writes one single-line JSON record (`{"type": "metric", "name": ..., "value": ..., "tags": {...}}`) to the container's stderr, riding Modal's OTEL integration into the tier's OpenObserve `modal_logs` stream -- the counting channel for expected, routine anomalies whose rate (not each occurrence) is the signal. Records carry the deployed env's name (`minds_env`) like the access-log lines.

- The structured-JSON access logging from `mngr/design-analytics-per-env` is carried here verbatim (one `{"type": "http_request", ...}` JSON line per request, the public `ensure_info_log_handler` helper, and `minds_env` stamping), so both branches converge on a single structured-JSON logging path; `metrics.py` uses the shared helper. The intra-package import this needs is now allowed by the shipped-imports guard (a sibling module always ships with the package).

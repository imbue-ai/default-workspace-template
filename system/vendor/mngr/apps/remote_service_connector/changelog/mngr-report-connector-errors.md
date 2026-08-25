Converts the connector to report unexpected exceptions to Bugsink correctly (building on the `mngr/env-tier-sentry` bring-up, which this branch also carries via merge until it lands on main):

- Every connector-defined exception now inherits a common `ConnectorError` base; the expected set is exactly what `raise_as_http` maps to status codes.

- Unexpected exceptions no longer get converted to `HTTPException(500, str(exc))` inside the routes: they propagate to a new app-level 500 handler that reports them to Bugsink at top priority and answers a structured `{"detail": {"code": "internal_error", "message": ..., "event_id": ...}}` body. The raw exception text is included only on dev/ci tiers -- production and staging clients no longer see internal exception text in 500 bodies (they get the Bugsink event id instead).

- `logger.warning` now reports to Bugsink as a warning-level event (the lower-priority channel for exceptions caught and tolerated for robustness); call sites across the connector were triaged accordingly -- tolerated-failure warnings gained `exc_info` stack traces, routine anomalies (attribution-cookie junk, transient Cloudflare/LiteLLM/SuperTokens hiccups, per-CA ACME failures) were downgraded to counted `metric` JSON log lines flowing into the tier's OpenObserve, and the relay health sweep's benign DNS-reconcile line dropped from error to warning.

- The `internal_error` body's `event_id` and `exception` fields are always present (empty string when unavailable/hidden), so a client shaped against dev/ci responses can never break in production over a missing key.

- New `GET /health/reporting-probe` (active on dev/ci tiers only; answers `{"status": "disabled"}` on production/staging): deliberately exercises every reporting channel in one request -- a metric line, a warning-level event, and an unmapped exception through the 500 handler -- driven by the new `minds_services` deployment test.

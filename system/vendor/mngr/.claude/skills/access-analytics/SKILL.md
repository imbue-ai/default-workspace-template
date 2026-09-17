---
name: access-analytics
description: The minds product-analytics lakes -- aggregate metrics in DuckLake, queried by attaching DuckDB locally with per-person credentials. Stub skill. Use for aggregate product questions ("how many users did X, trending how"); never for incident investigation -- the lakes hold no error text, no emails, and only redacted, consented workspace data.
---

# Accessing analytics (stub)

Analytics aggregates server-side product data (and consented, redacted explorer-workspace data) into per-env DuckLake lakehouses.
Analysts attach the lakes with DuckDB from their own machines using per-person credentials.
It is not an incident tool: exceptions live in Bugsink, request logs in OpenObserve, and neither emails nor error contents ever enter the lakes.

To get access, an operator mints per-person read credentials with `uv run minds-admin analytics analyst add <name>`.
The DuckDB attach snippet and worked example queries live in `apps/analytics/reports/README.md`.
The system overview lives in `apps/analytics/README.md`.

This skill is a living document; when information here is stale, follow the update protocol in `investigate-bug`.

## Related skills

- `investigate-bug` -- routes aggregate questions here and incident questions elsewhere.

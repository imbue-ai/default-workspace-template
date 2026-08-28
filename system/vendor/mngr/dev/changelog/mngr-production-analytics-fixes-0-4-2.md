# Workspace lock refresh for the analytics tenacity dependency

- The root `uv.lock` picks up `tenacity` (>=9.0, image pin 9.1.4), newly added to `apps/analytics` for the aggregation cron's transient-source retry.

- `style_guide.md`'s event-envelope section now requires `event_id` values to be unique per event globally, not just per machine (analytics dedupes fleet-wide by event id): mint a random id or hash in the full-precision timestamp, never derive one from only static values like a service name or URL.

- `specs/minds-analytics/redaction-contract.md` documents the transcript pipeline's new third scrub step: random-looking identifier tokens are replaced with `[REDACTED_TOKEN]`, with workspace-local paths (`/home/user...`, `~/...`) kept whole.

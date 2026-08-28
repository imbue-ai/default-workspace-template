Add `imbue.modal_app_kit.sentry`: shared sentry-sdk initialization for the Modal apps reporting to the per-tier self-hosted Bugsink instances.

`init_sentry` is idempotent per container, no-ops without a DSN or with `MINDS_SENTRY_DISABLED=1`, tags events with the service name / environment / deploy-id release, and installs a slim client-side dedup rate limiter (a stdlib-only port of the imbue_common one) plus an interrupt-event filter. `capture_and_reraise` covers Modal cron/spawned functions. Also adds a per-module allowance so sentry.py may import sentry_sdk (consumers must pin sentry-sdk in their image groups).

The `sentry-sdk` floor is 2.63.0: earlier FastAPI integrations re-wrap `dependant.call` on every request to a sync endpoint under FastAPI's lazy router inclusion, dying with RecursionError after ~990 requests in a warm container (mngr-internal#493).

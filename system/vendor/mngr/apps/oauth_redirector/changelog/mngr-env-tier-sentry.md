The redirector now reports errors to the dev/ci Bugsink instance via sentry-sdk. The DSN is baked at deploy time from the tier's `sentry` Vault entry when available (`just deploy-oauth-redirector` reads it best-effort), preserving the app's zero-Vault deployment story: an empty DSN simply disables reporting. `sentry-sdk` is pinned into the image.

`sentry-sdk` is pinned at 2.66.0 (not 2.59.0), picking up the fix for the per-request sync-endpoint wrapper leak (mngr-internal#493).

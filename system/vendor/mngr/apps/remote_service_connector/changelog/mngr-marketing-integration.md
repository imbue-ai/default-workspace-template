Merged the download-attribution feature branch (marketing attribution for account creations and app downloads -- the full feature is described in `mngr-download-attribution.md` in this directory, which lands in the same PR). Merge-specific changes on top of it:

- The migration creating the `account_attribution` and `download_events` tables was renumbered from 024 to 026, because main added `024_workspace_stop_start.sql` and `025_relays.sql` while the attribution branch was in flight.

- The connector README now documents the `GET /download` endpoint, the creation-time attribution capture, and the new `attribution.py` module, and links the cookie contract doc.

- The campaign-param allowlist gained `src` (the marketing site's per-button spot tag), so per-spot funnel queries need no raw-query parsing.

- Connector-synthesized touch timestamps are now `Z`-suffixed, millisecond-precision ISO 8601 (exactly JavaScript's `toISOString()` output, matching the cookie contract's examples), so every `at` string in the JSONB touches is uniform.

- The cookie contract doc was updated for the current origin layout (signup on `accounts.imbue.com`, downloads on `minds.imbue.com`, staging mirror on `imbue-staging.com`), the Netlify Edge Function implementation (imbue.com is deployed on Netlify, not behind a Cloudflare Worker), and the agreed consent policy (explicit banner choices mirrored to an `imbue_consent` cookie; geo-based EEA default at the edge).

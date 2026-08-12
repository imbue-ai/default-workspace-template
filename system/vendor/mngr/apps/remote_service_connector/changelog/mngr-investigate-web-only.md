Support for the hosted, browser-only minds client:

- The accounts broker gained an owner fast path: when the signed-in browser session owns the share (its user id matches the share record's owner), `/share/authorize` skips the "Continue as ..." interstitial and the verified-email gate, and mints the handoff JWT with an `owner: true` claim. Non-owner visitors are unchanged (interstitial + verified email still required).

- New `POST /hosts/{host_db_id}/enable-sharing` endpoint: the web client cannot inject share materials itself (no SSH in the browser), so the connector does it server-side with the pool key. It creates/rotates the share record, then writes `share.env` (including the new `SHARE_CHROME_ORIGIN`, which lets the hosted chrome embed the workspace) and an owner-granted `share_grants.toml` into the workspace container. Idempotent; only for the caller's own leased hosts.

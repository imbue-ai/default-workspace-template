The share endpoints now report the tier's hosted web-chrome origin, so desktop clients can stamp the correct `SHARE_CHROME_ORIGIN` into a shared workspace's `share.env` (issue #746: desktop-created shares stamped the bare connector origin into `frame-ancestors`, locking the real `/web` chrome out on tiers with custom domains).

- `POST /shares` and `GET /shares/{host_id}/status` carry a new optional `chrome_origin` field: the connector's own `SHARE_CHROME_ORIGIN` env value (the same value web-created workspaces already get), or `null` when the tier has none configured. Additive-with-defaults on tolerant-client endpoints, so no new wire-compat snapshot is needed and old clients are unaffected.

- The env read moved from a private helper in `hosts.py` to `shares.share_chrome_origin()`; the server-side enable-sharing path uses the shared helper (no behavior change there).

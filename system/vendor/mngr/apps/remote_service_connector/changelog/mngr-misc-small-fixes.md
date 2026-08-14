Sharing fixes across the connector and its web surfaces:

- The connector no longer SSHes into workspaces to read `apps.toml` for a share's chrome entry origin. The frps `NewProxy` callback now records the shell service's label from the tunnel's own (already-authorized) hostname claims, so the connector needs no access into the workspace at all for this. Claim and enable-sharing responses report `entry_label` as null until the tunnel connects; the web chrome's create flow and workspace view re-resolve it from share status while waiting.

- `POST /shares` accepts an optional `preferred_region`, honored only for hosts the connector has no datacenter record of (local workspaces) and only when a relay serves that region. A share's region is now sticky: a re-share always keeps the existing row's region (the region is baked into the workspace domain, so it must never silently move). New `GET /shares/relays` endpoint serves the region -> relay endpoint map so clients can pick by measured latency.

- Share visitors who have to verify their email no longer dead-end: the broker's check-inbox redirect carries the share-authorization return path, the check-inbox page polls the session's verification state and continues to the workspace automatically, and the verification email's link itself carries the same return path so the verify-email page offers "Continue to the shared workspace". Only local `/share/authorize` paths are ever honored.

- The web overview (`/web`) hides destroyed workspaces by default behind a "Show N destroyed workspaces" toggle.

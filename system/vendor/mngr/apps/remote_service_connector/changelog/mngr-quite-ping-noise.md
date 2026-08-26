frps Ping heartbeat auth no longer dominates connector latency, DB load, and log volume (mngr-internal#617):

- Every store now checks Neon connections out of a per-container pool (`db.pooled_db_connection`) instead of opening a fresh TLS connection per call, dropping the ~770 ms per-ping connection-setup cost to a warm reuse for every endpoint. Broken connections are discarded (with a `db_pooled_connection_discarded` metric) and roll back on check-in.

- Allowed Ping decisions are cached in-process for `MINDS_FRPS_PING_CACHE_TTL_SECONDS` (default 30 s, `0` disables), turning per-ping DB reads into one read per live token per TTL per container. The live-tunnel kill switch for suspended/unshared workspaces now takes effect within one heartbeat interval plus at most the TTL; rejects and fail-open errors are never cached.

- Successful pings emit no per-request structured access-log line; their rate and handling duration flow as periodic per-relay metric records (`frps_ping_authorized` count and `frps_ping_authorized_duration_ms_total`), flushed roughly per minute and on graceful container shutdown via the app lifespan. Rejected and errored pings still log in full.

- The frps plugin-auth shared secret (a path segment of `/frps/auth/...`) is now redacted from the connector's structured access-log lines.

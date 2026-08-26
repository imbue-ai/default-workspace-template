`RequestLoggingMiddleware` gains two route-controlled scope-state keys (for the connector's frps heartbeat traffic, mngr-internal#617):

- `ACCESS_LOG_SUPPRESS_SUCCESS_STATE_KEY` suppresses the structured access-log line for 2xx responses only, so high-frequency machine traffic can be counted by periodic metric records instead of one line per request; non-2xx and raised outcomes always log in full.

- `ACCESS_LOG_PATH_OVERRIDE_STATE_KEY` replaces the logged request path with a sanitized form for routes whose real path carries a credential in a path segment (the frps plugin-auth shared secret).

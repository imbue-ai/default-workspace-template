The gateway's access log no longer records query strings. Every sign-in through a share logged the full `/_auth/callback?token=<JWT>` request line, so the handoff token (with the visitor's email) sat in `share-gateway.log`. A bug report's secret scan then flagged that log and withheld the workspace's logs from the report. A request with a query string is now logged as `GET /_auth/callback?<query omitted>`.

Lines already in an existing `share-gateway.log` stay there until the log rotates.

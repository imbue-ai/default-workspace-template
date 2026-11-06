The share gateway records what the share served (`specs/minds-app-usage-signals/spec.md` in the mngr repo):

- caddy writes a per-request access log to `data/.state/share_gateway/access.log`: the visitor's address, the origin, the method, the path, the status, the size and the duration, with every request header, response header and query string deleted by caddy's log filter (the session cookie, admission tokens, handoff tokens and an app's own credentials all travel there). It rolls at 8 MiB with 4 rolled files kept for at most 30 days.

- The verify handler counts every request it answers, keyed by app, requester kind and id, method class and outcome (allowed, or a denial reason), and a flusher thread writes one record per key per minute to `$MNGR_AGENT_STATE_DIR/events/gateway_requests/events.jsonl` under the standard envelope, which the explorer analytics collection reads. At most 256 keys per minute (the rest fold into one overflow record); the stream archives itself at 32 MiB through the new `event_streams` library.

- The gateway's own werkzeug request line is gone (caddy's log supersedes it); denials are still logged.

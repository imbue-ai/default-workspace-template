Multi-relay tunnels (blueprint/multi-relay phase 1 in the mngr repo): the
share gateway now runs one frpc per relay in the workspace's region instead
of exactly one, so any relay can serve any visitor and a dead relay fails
over via DNS + browser address fallback.

The relay set comes from the connector's new relay-token-authenticated
`GET /shares/assignment` endpoint -- fetched at stack start, re-polled on the
server-provided interval (fleet changes converge without touching the
workspace), and cached at `data/.state/share_gateway/assignment.json` so a
container restart brings tunnels up with the connector unreachable.
`share.env` no longer carries `SHARE_RELAY_ENDPOINT`.

Rendered frpc configs pin `loginFailExit = false` (one relay being down must
not kill its tunnel process) and 10s/30s heartbeats (bounding how long a
wedged relay holds a tunnel); a single frpc dying now restarts just that
tunnel rather than the whole stack.

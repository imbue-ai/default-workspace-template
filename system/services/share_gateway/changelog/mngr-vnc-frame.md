The share gateway brokers the Screen's stream for visitors (`docs/system/specs/screen/spec.md` section 15, `specs/turn-relay/spec.md` section 7, `specs/screen-studio-integration/spec.md` section 6.3):

- Every relay's frpc config renders a second proxy, `screen-media` of type `tcp`, `localPort` 59000 (neko's TCP ICE mux) and `remotePort` the assignment's `media_port`, when the assignment carries one; none otherwise.

- `GET /shares/assignment` is parsed additively: `media_port` and `published_endpoints` are read when present, and an older connector's answer without them still works (the cache round-trips both).

- The gateway fetches `GET /turn/ice-servers` from the connector with the relay token at stack start and at half life, keeps the credential in memory only, serves an empty list after a refusal or an unreachable connector (retrying five minutes later, and keeping a credential already held until it expires), and never fails the share over it.

- `GET /connect-info` on the loopback port 8791, beside `/_auth/*`, answers the Screen service `{"ice_servers", "candidates"}`: the credential as fetched, and the assignment's `published_endpoints` plus one `tcp` candidate per relay address at `media_port`, each with its `guest_port`. caddy never routes a visitor to it.

- The `screen-media` proxy delivers a visitor's connection to neko's TCP mux at the workspace's own interface address, not loopback: neko's ICE stack (pion) keys its TCP mux by the local address a connection arrives on and gathers a candidate for the interface alone, so a connection spliced to loopback was accepted and then read by nothing, and a visitor of a shared local workspace never got past the screencast. The address is found at render time (`local_address.py`); a workspace with no routable address renders no media proxy.

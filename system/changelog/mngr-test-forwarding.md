The agent-facing `layout.py` helper now recognizes service panels by their
unguessable per-service origin label (`<name>-<rand>`). Stored panel URLs carry
this random label as their leading hostname component, so `layout.py` maps it
back to the service name via the app registry (`data/.state/apps.toml`) when
matching `service:` refs against the live layout.

Surface real client IPs at the share gateway: frpc now stamps each spliced connection with a PROXY protocol v2 header carrying the address the relay saw, and the rendered Caddyfile consumes it via a loopback-only `proxy_protocol` listener wrapper (ahead of the tls wrapper). The gateway logs every denied or unauthenticated-non-HTML request with that real address, so scanner probes and revoked visitors are distinguishable from frpc's loopback in the share-gateway service log. Allowed requests stay unlogged.

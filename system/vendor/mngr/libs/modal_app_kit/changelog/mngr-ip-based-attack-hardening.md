# Client IP derivation trusts only the socket peer

- `client_ip_from_asgi_scope` (the request-logging middleware's client-IP source, now also feeding the connector's signup IP gate) derives the client IP exclusively from the ASGI socket peer. Empirical verification against Modal's ingress showed the peer carries the real end-client IP while `X-Forwarded-For` is stripped -- but other forwarding-style headers (`X-Real-IP`, `Forwarded`, `CF-Connecting-IP`) pass through unsanitized, so the previous first-XFF-hop preference was a latent spoofing vector and is removed.

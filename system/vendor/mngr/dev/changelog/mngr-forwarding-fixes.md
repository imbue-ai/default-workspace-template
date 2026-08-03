Add the `blueprint/sharing-redesign/` spec: a plan to replace Cloudflare tunnel/Access sharing with self-hosted infrastructure (SNI-passthrough frp relays on OVH, in-workspace TLS termination and auth gateway, imbue-account login via SuperTokens, ACME DNS-01 certs via the connector, PSL-listed per-user site isolation) and to redo local forwarding as service-per-origin with full host ids. Spec only; no behavior changes.

Add `just render-share-relay` and `just test-share-relay` recipes for the new `share_relay` project.

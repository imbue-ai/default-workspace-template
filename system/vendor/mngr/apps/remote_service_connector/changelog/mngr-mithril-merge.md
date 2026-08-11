Merge main into the mithril-refactor stack, reconciling the relay-based sharing rewrite with main's mechanical split of the connector into modules.

The relay-sharing code now lives in per-feature modules following main's layout: `shares.py` (share records, relay tokens, frps plugin auth), `share_certs.py` (ACME DNS-01 issuance), and `share_broker.py` (the accounts broker, including the browser Google sign-in).

The Cloudflare tunnel/Access/KV forwarding stack that main had split into `forwarding.py`, `tunnels.py`, and `naming.py` is deleted (it was replaced by relay sharing on this branch); `cloudflare.py` keeps only the R2 API client plus the shared `CloudflareCtx` container context.

Request auth is SuperTokens-only (the tunnel-token Bearer path is gone), the tunnel quota entitlements are dropped, and `complete_oauth_code_exchange` is extracted in `auth_proxy.py` so the CLI OAuth callback and the broker's browser flow share one implementation.

The new image dependencies (acme, josepy, pyjwt, cryptography) are ==-pinned in the image dependency group, added to the allowed import roots, and baked into the regenerated hash-locked `image_requirements.txt`.

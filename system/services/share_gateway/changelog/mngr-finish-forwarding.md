New `share_gateway` service: the in-workspace half of the self-hosted sharing redesign.

While share materials (`data/.secrets/share.env`, written by the minds app) are present, the runner keeps the whole share stack up: caddy terminating the workspace's real TLS certificate locally (the relay never sees plaintext), an frpc tunnel to the region's relay authenticated by the per-share relay token, and a forward_auth gateway that checks the `imbue_machine_session` cookie, re-reads the owner's grants file on every request (instant revocation; malformed grants fail closed), enforces the WebSocket/non-GET Origin policy, and strips the session cookie before requests reach services.

Visitors without a session are redirected to the accounts broker; the login callback verifies the broker's 60-second audience-bound handoff token (JWKS, nonce, single-use jti) and sets a 24-hour workspace-domain cookie.

The TLS private key is generated in the workspace and never leaves it; the connector signs CSRs via ACME DNS-01, and a daily check renews certificates within 30 days of expiry.

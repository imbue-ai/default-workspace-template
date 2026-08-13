Merge the web-only workspace/accounts work (`mngr/hopefully-last-web-details`) into main. Root-level pieces:

Added the `blueprint/minds-web-client/` plan and running `HANDOFF.md` for the hosted, browser-only minds client at minds.imbue.com (browser-orchestrated create over the connector claim endpoint, workspace access via the share stack with an owner fast path, the in-workspace `owner-exec` Ed25519-signed exec service, DEK-in-browser sync writes, and the grants single-writer migration).

Added the `blueprint/web-account-creation/` plan: move all minds sign-up/sign-in to a connector-hosted, Minds-branded web accounts surface with a loopback+PKCE handoff back to the desktop app/CLI, non-blocking email verification, and a per-tier OAuth redirector.

New `just provision-dev-relay` recipe plus `scripts/provision_dev_relay_config.py`: stands up an activated dev/ci env's own share relay in one shot (OVH instance + frps pointed at that env's connector + region DNS), pulling credentials from the tier's Vault entries.

New `just deploy-oauth-redirector <tier>` recipe for the once-per-tier redirector deploy, plus secret-schema updates (`TURNSTILE_SITE_KEY`/`TURNSTILE_SECRET_KEY` in `supertokens.sh`; `OAUTH_REDIRECTOR_URL`/`ACCOUNTS_COOKIE_DOMAIN` in `sharing.sh`) and the root pyproject's `oauth_redirector` coverage flag + connector import-layers contract entry.

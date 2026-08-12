Add the `blueprint/web-account-creation/` plan and its repo-level implementation pieces: move all minds sign-up/sign-in to a connector-hosted, Minds-branded web accounts surface (merged with the share-broker login) with a loopback+PKCE handoff back to the desktop app/CLI, make email verification non-blocking behind a `require_verified_email` guard (share visits and ally eligibility only), add a per-tier OAuth redirector for dev/CI, and delete the desktop app's legacy JinjaX auth pages.

- New `just deploy-oauth-redirector <tier>` recipe (private.just) for the once-per-tier redirector deploy.

- Secret schema updates: `TURNSTILE_SITE_KEY`/`TURNSTILE_SECRET_KEY` join `.minds/template/supertokens.sh`; `OAUTH_REDIRECTOR_URL` and `ACCOUNTS_COOKIE_DOMAIN` join `.minds/template/sharing.sh`.

- Root pyproject gains the `oauth_redirector` coverage flag and lists `accounts_web` in the connector's import-layers contract.

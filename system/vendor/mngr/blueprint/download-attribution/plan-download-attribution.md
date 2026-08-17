# Download attribution: linking marketing campaigns to account creation

## Overview

- Attribute minds account creations (and app downloads) to marketing campaigns via a first-party `imbue_attribution` cookie set on `.imbue.com` by the marketing site and read by the connector's signup surface at `minds.imbue.com` (the already-planned Modal custom domain for the production connector).

- The cookie bridges the app-download gap without any installer-side mechanism: the desktop app signs up by opening the user's default system browser -- the same browser that visited imbue.com from the campaign link -- so the `.imbue.com` cookie is presented at signup.

- The cookie is set server-side at the edge (a Cloudflare Worker on imbue.com, out of scope here) so Safari ITP's ~7-day cap on script-written cookies does not apply; imbue.com's existing consent banner gates whether it is set at all. Its contract is pinned in a standalone shareable doc in this repo.

- Attribution is captured only at account *creation* (never sign-in), on both creation paths: the browser email-password signup and the Google OAuth callback (using the SuperTokens `created_new_recipe_user` flag, which `complete_oauth_code_exchange` currently discards).

- A `GET /download` route on the connector records campaign-tagged download events and 302s to the stable per-platform ToDesktop links, giving the funnel its denominator. All data (attribution rows + download events) lands in the connector's Neon DB; reporting is plain SQL, no admin surface.

- Connector-side capture and `/download` ship now: the signup-page query-param path works on any origin immediately, and the cookie path lights up when the `minds.imbue.com` domain and the marketing worker land.

## Expected behavior

### Cookie (written by the marketing site, read by the connector)

- One JSON cookie, `imbue_attribution`, `Domain=.imbue.com; Secure; SameSite=Lax; Path=/`, ~90-day Max-Age, refreshed on every write.
- Top-level fields: schema version, an anonymous random visitor id (minted on first write, stable thereafter), a `first` touch (written once, never overwritten), and a `last` touch (overwritten on every non-direct touch -- i.e. any landing carrying campaign params or an external referrer; direct visits never update it).
- Each touch stores the allowlisted params (`utm_source`, `utm_medium`, `utm_campaign`, `utm_term`, `utm_content`, `gclid`, `fbclid`), the referrer, the landing path, a timestamp, and the full raw query string (length-capped) for future-proofing.
- Consent-declined visitors get no cookie; everything below degrades gracefully to query-param-only attribution or unattributed rows.

### Signup capture

- When an account is created via the hosted pages (email-password form or Continue-with-Google), the connector writes one `account_attribution` row: user id, email, visitor id, `first`/`last` touch blobs, `signup_context`, signup method, and timestamp. Exactly one row per account, written once; sign-ins of existing accounts never write or update it.
- The signup page forwards allowlisted campaign params from its own URL in the signup request; they overwrite the cookie's `last` touch (the signup URL is by definition the latest touch) and synthesize the sole touch when the cookie is absent.
- `signup_context` is derived from the login page's `next=` target: `desktop_app` (`/accounts/authorize`), `share_visit` (`/share/authorize`), `web_chrome` (`/web...`), `web` (everything else). This distinguishes "downloaded the app then signed up" from "signed up on the web".
- For Google signups, the campaign params and context survive the OAuth round-trip (they ride the `next` path that already flows through the OAuth state), and the row is written only when the exchange actually created a new user. The one-account-per-email guard runs before creation, so `created_new_recipe_user=True` reliably means a brand-new account.
- Capture fails open: a failed attribution write logs a warning and the account creation still succeeds.
- The deprecated JSON auth endpoints (CLI/headless signup) are not instrumented; those signups are simply unattributed.
- The web-only minds client needs no special handling: it sends signed-out users to the same `/login`/`/signup` pages on the same origin, so cookie and param capture work identically for desktop and web-only funnels.

### Download route

- `GET /download?platform=<value>` records a `download_events` row (visitor id + touch blobs from the cookie when present, platform, user-agent string -- no IP, timestamp) and 302s to the target:
  - `mac-arm64` (alias: `mac`) -> the stable ToDesktop link `https://dl.todesktop.com/26032588hqdzk/mac/dmg/arm64`.
  - `source` -> `https://github.com/imbue-ai/mngr` (the escape hatch for platforms without builds).
  - Unknown or missing `platform` -> 404.
- Allowlisted campaign params on the `/download` URL itself are folded into the event as a synthesized touch when the cookie is absent, so consent-declined downloads still get campaign-tagged.
- The route fails open: the redirect always happens; a failed event write logs a warning and loses that row.

### Verification and reporting

- Funnel questions (campaign -> download -> signup, joined exactly via visitor id) are answered with SQL against Neon; no admin endpoint.
- The end-to-end cookie flow is verified manually on production after deploy (hand-set cookie, test-account signup, check the row). Staging gets no imbue.com subdomain for now.

## Changes

All in `apps/remote_service_connector` unless noted.

- **New migration** (`migrations/026_account_attribution.sql`): `account_attribution` table (one row per account: user id, email, visitor id, first/last touch JSONB, signup context, method, created-at) and `download_events` table (visitor id, touch JSONB, platform, user-agent, created-at). Runs automatically via the existing `schema_migrations` runner at `minds env deploy`.

- **New attribution module**: parse/validate the `imbue_attribution` cookie (tolerant: malformed or oversized cookies log a warning and count as absent), the touch allowlist, the merge rule (cookie first/last + signup-page params overwriting `last` / synthesizing the sole touch), and the two Postgres writers. Both writers fail open.

- **`accounts_web.py`**: after a successful `ep_sign_up`, write the attribution row from the request's cookie + the new optional campaign fields on the signup body. In the Google OAuth callback, do the same when the exchange created a new user. Add the `GET /download` route (platform map, aliasing, event write, 302).

- **`auth_proxy.py`**: thread `created_new_recipe_user` out of `complete_oauth_code_exchange` (additive -- the deprecated JSON endpoints' wire shape is unchanged), and carry the campaign params/context through the browser OAuth flow alongside the existing `next` handling.

- **Accounts frontend (`frontend/src/`)**: on signup submit, include allowlisted params from `location.search` and the `signup_context` derived from `next` in the POST body; preserve them across the Google redirect (they already live in the page URL that `next` returns to).

- **New shareable contract doc** (`docs/attribution-cookie-contract.md`): everything the imbue.com side needs -- cookie name, JSON schema, set/update rules (first vs. last non-direct touch, visitor id, Max-Age refresh, consent gating, edge-set requirement), plus the `/download` and signup link URL formats. Linked from the PR.

- **Tests**: unit + integration only, against the existing fake SuperTokens backend (which already models `created_new_recipe_user`): cookie parsing/merge rules, both signup paths writing exactly one row (and none on sign-in), OAuth created-vs-existing, `/download` redirects + event rows + fail-open, context derivation. No deployment test for now.

- **Out of scope**: the imbue.com Cloudflare Worker (contract only), the `minds.imbue.com` custom-domain provisioning (already tracked in `apps/minds/docs/next_deploy.md`, including `ACCOUNTS_COOKIE_DOMAIN` / Turnstile allowlist / OAuth redirect updates), and any admin/reporting surface.

# Web account creation

Move all minds sign-up/sign-in to the web: a Minds-branded accounts surface hosted by the
remote service connector (merged with the share-broker login), with a loopback+PKCE handoff
back to the desktop app / CLI. Email verification becomes non-blocking (a small
`require_verified_email` guard applied only where email is an authorization identity), and
the desktop app's legacy JinjaX auth pages are deleted.

## Overview

- Sign-up/sign-in leaves the desktop app entirely: every auth entry point opens the
  system browser onto a connector-hosted accounts page, and the app/CLI receive the
  SuperTokens session via a loopback redirect with a one-time code + PKCE exchange
  (generalizing the pattern the plugin's OAuth flow already uses).
- The share-broker login and the new app login merge into one accounts surface at the
  tier's accounts URL (`accounts.imbue.com` / staging twin, already live; dev/ci tiers use
  the bare connector URL). Browser sessions become first-class: SuperTokens' native
  cookie-based session management, ~30-day rolling lifetime, `Domain=imbue.com` (host-only
  fallback on modal.run) — the first concrete step toward a purely web-based Minds.
- Email verification stays real but stops blocking: the connector authenticates unverified
  accounts, and a reusable `require_verified_email` guard gates only share-visit
  authorization and ally-plan eligibility. Nothing ever auto-marks an email verified
  (the paid-list auto-verify paths are removed); verification emails send contextually at
  the first gated action instead of at signup.
- This unblocks deleting the app's remaining server-rendered auth surface
  (`templates/auth/*`, the sign-in modal, `static/auth.js`) and the plugin's
  pending-session machinery — cleanup after the Mithril refactor.
- Dev and CI tiers get browser OAuth with zero per-env registration via a fixed per-tier
  OAuth redirector (one registered redirect URI per tier on the shared Google client).
- Everything lands on one branch, merged when the flow works end-to-end. Released desktop
  builds keep working against the connector's existing JSON auth endpoints, which stay up
  marked deprecated until the new client is fully rolled out.

## Expected behavior

### Signing up / in from the desktop app

- Every auth affordance (welcome page Sign Up / Sign in, the create flow's signed-out
  Imbue Cloud preset, Add account, the `auth_required` share prompt) opens the hosted
  accounts page in the system browser and shows an in-app Mithril waiting modal with a
  cancel button and a "copy sign-in link" fallback for browsers that fail to launch.
- Completing auth in the browser lands on a Minds-branded success page ("You're in",
  with an "Open app" link); the app detects completion, finishes the local sign-in
  bookkeeping (session persisted by the plugin, provider registration, observer bounce)
  and navigates on — same landing decisions as today's `/post-login`.
- A brand-new email/password signup counts as signed in immediately — no
  check-your-email interstitial, no pending state. Multi-account in the app is unchanged;
  each handoff mints its own device session.
- Signing out of the app revokes only that device's session; other devices and the
  browser session survive. "Sign out of all devices" lives on the hosted account page.
- A new client pointed at a connector without the new endpoints (stale dev env) shows a
  clear actionable error ("this env's connector is too old — run `minds env deploy`");
  there is no in-app fallback flow.

### The hosted accounts surface

- Minds-branded throughout; serves email/password sign-in/sign-up, "Continue with
  Google", forgot-password, and a Cloudflare Turnstile challenge on the signup form.
- When a browser session already exists, every authorization handoff — app login and
  share visit alike — shows a one-click "Continue as `<email>` / Use a different account"
  interstitial; nothing is ever silent. The browser holds a single live session; "Use a
  different account" signs it out and shows the login form.
- A signed-in user landing with no pending handoff sees a minimal account page: email,
  verified badge (or "verify now"), change password, sign out, sign out of all devices,
  and a "download Minds" pointer.
- The existing bare-HTML utility pages (verify-email result, password-reset form, the
  share flow's check-your-inbox page) are restyled into the same bundle.
- Share visits keep working through the same surface: the workspace gateway's redirect
  lands on the merged login, and the handoff JWT flow to the gateway is unchanged. Old
  `imbue_sso_session` cookies are simply ignored (one extra login after cutover).

### Email verification

- Signup sends no verification email and blocks nothing. Google OAuth accounts are
  verified by construction (provider-attested); the paid list no longer auto-verifies
  anyone.
- Only two actions require a verified email: satisfying a share grant as a visitor, and
  ally-plan eligibility (explicit switch and the lazy pre-cutoff backfill). Gated
  endpoints return a structured 403 (`code: email_not_verified`).
- When an in-app action hits that 403, the app auto-sends the verification email (server
  cooldown bounds it) and shows "we just sent a link to …" with a resend button. A
  share-visit signup sends immediately — that flow's signup *is* the gated action.
- Any other endpoint can be flipped later by adding the one-line guard (first candidate
  if pool abuse appears: `POST /hosts/lease`).

### CLI and compatibility

- `mngr imbue_cloud auth login` is the browser flow; `auth signin` / `auth signup`
  (password, headless) stay documented and supported; `auth oauth` is deleted. Unverified
  accounts appear in `auth list` immediately; `is-verified` becomes a plain status query.
- The connector's existing JSON auth endpoints stay for released clients, marked with
  "Deprecated:" docstrings plus a removal-condition note in the connector README ("after
  all desktop clients ship the new login flow"). After removal, JSON/CLI signup remains
  enabled only on dev and CI tiers; prod/staging signups require the browser, and an
  admin-key-authenticated test-signup endpoint (with a `verified` flag) keeps deployment
  tests working on every tier.

## Changes

### Connector (`apps/remote_service_connector`)

- Relax `_authenticate_supertokens`: resolve the email regardless of verification, carry
  `is_email_verified` on `UserAuth`, and stop rejecting unverified users; add a small
  `require_verified_email` guard used by the share-broker authorize path and both ally
  eligibility paths; keep a verified-only email resolver for those consumers.
- Switch the emailverification recipe to `mode="OPTIONAL"`; remove the signup paid-list
  auto-verify branch and `mark_paid_email_verified_best_effort`; stop sending
  verification emails at signup/signin (the send-with-cooldown machinery stays for
  contextual sends).
- New accounts surface: hosted login/signup/interstitial/account-page routes serving the
  built frontend bundle; one-time authorization-code issuance on successful login
  (loopback `redirect_uri` validation + PKCE) and a code-exchange endpoint returning the
  access/refresh pair; a session-scoped revoke endpoint for app sign-out; Turnstile
  server-side verification on signup; the admin test-signup endpoint.
- Mount SuperTokens' native session management (cookie transfer) for the accounts routes
  on a base path that does not collide with the deprecated hand-rolled `/auth/*`
  endpoints; ~30-day rolling sessions, cookie `Domain=imbue.com` with host-only fallback
  for tiers without an accounts domain.
- Rework the share broker onto the merged surface: `/share/authorize` consumes the new
  session, always shows the interstitial, and keeps minting the same handoff JWTs; the
  hand-rolled login page, SSO cookie, and broker-specific Google flow are deleted.
- Mark the legacy JSON auth endpoints deprecated (docstrings + README removal condition).

### Hosted frontend (`apps/remote_service_connector/frontend/`)

- New small Vite/Mithril/Tailwind package copying the minds theme tokens; pages: login,
  signup (with Turnstile), continue-as interstitial, account page, forgot/reset password,
  verify-email result, check-your-inbox, and the handoff success page copy.
- `minds env deploy` builds the bundle before `modal deploy` (fails fast with a clear
  error when pnpm/node is missing); the built assets are added to the connector's Modal
  image as one extra image instruction, consistent with the pinned-image conventions.

### Per-tier OAuth redirector

- A tiny standalone Modal app deployed once per dev/ci tier workspace via a `just` recipe
  + runbook note; forwards the Google callback to the per-env connector callback carried
  in signed state, restricted to hosts matching the tier's connector URL pattern. The
  shared dev/ci Google clients register only this one redirect URI; prod/staging register
  their stable accounts domains directly.

### Plugin (`libs/mngr_imbue_cloud`)

- New `auth login` command: loopback listener + PKCE, opens the hosted page, exchanges
  the code, persists the session through the existing store; `--no-browser` prints the
  URL (the copy-link fallback). Delete `auth oauth`.
- Delete the pending-session machinery (`is_pending_verification` gating in the store,
  `auth list` filtering, promotion logic); `is-verified` reports status only.
- Sign-out uses the new session-scoped revoke.

### Minds app (`apps/minds`)

- Replace every auth entry point with a backend "launch web login" endpoint (runs
  `mngr imbue_cloud auth login` in a background thread, reusing the OAuth flow-status
  tracker) plus a Mithril waiting modal with cancel and copy-link.
- Delete the legacy auth surface: `templates/auth/*`, `pages/SigninModal.jinja`,
  `static/auth.js`, `templates_auth.py`, the `/auth` page routes and OAuth flow routes in
  `supertokens_routes.py` (sign-out/status move to `/ui/api/*`), the Electron sign-in
  modal WebContentsView, and the `/auth/`-page special-casing in `handleAuthEvent`.
- Delete the check-email flow and signup/signin verification deferral; add the contextual
  verify-email prompt (auto-send + resend) where `email_not_verified` 403s surface (plan
  switch); add the too-old-connector error.

### Config, secrets, docs, tests

- `TURNSTILE_SITE_KEY` / `TURNSTILE_SECRET_KEY` join the `supertokens.sh` schema (empty =
  Turnstile disabled, for dev/tests); `ACCOUNTS_BASE_URL` and `ClientEnvConfig.accounts_base_url`
  are reused as-is.
- Playwright-driven end-to-end test of the hosted page (signup with Turnstile test keys,
  login, loopback handoff) against a ci env; unit/integration tests for code exchange,
  PKCE validation, session issuance/revocation, the verified-email guard, and the
  no-auto-verify rule; deployment tests updated (mail.tm verification coverage kept);
  interactive app-side waiting-modal behavior verified manually via tmux, not pytest.
- Changelog entries for every touched project (`remote_service_connector`, `mngr_imbue_cloud`,
  `minds`, `dev` if root tooling changes); update the connector README, the desktop-client
  README, and `docs/latchkey-permissions.md`-adjacent auth docs where they describe the
  old flow.

## Open questions

- Exact page copy and visual design for the hosted surface (settled at build time against
  the minds design tokens).
- Whether the interstitial-on-every-share-visit proves too much friction in practice
  (revisit after real use; the mechanism trivially supports making repeat visits silent).
- Turnstile on sign-in as well as signup (start signup-only; add if credential-stuffing
  appears).
- The concrete trigger for removing the deprecated JSON endpoints (client adoption
  metric vs a fixed release count).
- Sign-in-link lifetime for the copy-link fallback (must balance SSH-user convenience
  against leaving long-lived auth URLs in clipboards).

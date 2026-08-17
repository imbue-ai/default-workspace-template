Add the hosted accounts surface and make email verification non-blocking.

- New Minds-branded browser pages at `/login`, `/signup`, and `/manage`, served from a built frontend bundle (`frontend/`, attached to the Modal image by `minds env deploy`), with a JSON API under `/accounts/api/*` (signin/signup with Cloudflare Turnstile, signout, signout-all-devices, change-password, contextual verification send, me/config).

- Browser sessions use SuperTokens' native cookie session management (middleware mounted at `/accounts/auth`; cookie domain configurable via `ACCOUNTS_COOKIE_DOMAIN`).

- New device handoff for the desktop app/CLI: `/accounts/authorize` mints single-use, PKCE-bound one-time codes (Neon table, migration 022) and `POST /auth/device/token` exchanges them for a fresh device session. Every handoff requires an explicit user confirmation ("Continue as ..."), never a silent redirect.

- The share broker merges onto the same surface: `/share/login` permanently redirects to `/login`, `/share/authorize` resolves the shared browser session, and browser Google OAuth moves to the accounts surface (callback path unchanged) with per-tier redirector support via `OAUTH_REDIRECTOR_URL`.

- Email verification is now non-blocking: unverified accounts authenticate normally; only share visits and ally-plan eligibility require a verified email (structured `email_not_verified` 403 via the new `require_verified_email` guard). Signup/signin no longer send verification emails (contextual sends remain), and the paid-list auto-verification paths are removed -- nothing marks an email verified without a clicked link (the admin-key `POST /admin/test-signup` test endpoint is the sole operator exception).

- New `POST /auth/session/revoke-current` for device-scoped sign-out. The old JSON auth endpoints (`/auth/signup`, `/auth/signin`, the OAuth pair) are deprecated in favor of the browser flow; see the README's "Deprecated JSON auth endpoints" section for the removal condition.

- Browser sessions carry an absolute ~30-day lifetime cap enforced by the connector: the session's creation time is stamped into its access token payload (surviving refreshes) and any session past the cap -- or without a readable stamp -- is revoked at resolution time.

- The email-utility pages move into the hosted bundle: `GET /auth/verify-email` and `GET /auth/reset-password` now serve the Minds-branded frontend (with a new `POST /accounts/api/verify-email` consuming verification tokens), and the share flow's check-your-inbox page becomes the bundle's `/check-inbox` page (the broker 303s there).

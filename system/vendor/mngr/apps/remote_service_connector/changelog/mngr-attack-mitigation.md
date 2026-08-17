# Sign-up abuse and spam mitigations

- The connector now logs one structured access-log line per request to the Modal function logs (method, path without the query string, status, duration, client IP from the first `x-forwarded-for` hop, user agent), via the shared `modal_app_kit` request-logging middleware.

- Creating a remote workspace (`POST /hosts/lease` and `POST /hosts/claim`) now requires a verified email address. An unverified account gets the structured `email_not_verified` 403 whose message says to check the inbox and spam folder, and the refusal itself sends the verification email server-side (under the existing per-user cooldown), reported in the detail's `sent` field.

- `POST /auth/signup` is disabled on production and staging (status `SIGNUP_DISABLED`): account creation on those tiers goes through the browser accounts surface, whose signup form carries the Turnstile bot gate. Dev/CI tiers keep the headless JSON signup, and the admin-key `POST /admin/test-signup` works everywhere. Sign-in is unchanged on every tier.

- The deprecated JSON OAuth pair (`POST /auth/oauth/authorize` + `POST /auth/oauth/callback`) is removed; Google sign-in exists only as the accounts surface's browser flow.

- On the hosted sign-in/sign-up page, "Continue with Google" is now the visually dominant way to create an account: the sign-up tab keeps the email/password fields collapsed behind a "Use email and password instead" link (expanded automatically on tiers without Google configured), and the sign-in tab keeps the Google button on top of the visible credentials form. A `password_account` bounce from the Google flow now lands on the sign-in tab, where its remedy (the password form) is visible.

- The hosted web chrome surfaces structured API refusals (e.g. `email_not_verified`, `quota_exceeded`) as their human-readable message instead of a raw JSON blob.

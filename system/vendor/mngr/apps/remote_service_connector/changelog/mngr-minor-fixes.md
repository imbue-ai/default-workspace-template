Enforce one account per email across login methods at signup time.

Previously, an email that already had a Google (or other OAuth) account could sign up again with email + password, and vice versa, producing two unrelated SuperTokens users with the same email (each with its own workspaces, keys, and entitlements). This happened twice in production.

`/auth/signup` now refuses when the email is already registered under a different login method, and `/auth/oauth/callback` refuses before creating the third-party user when the email already has a password (or other-provider) account. Both return the new `ACCOUNT_EXISTS_WITH_OTHER_METHOD` status with a message naming the method to sign in with instead.

Pre-existing cross-method duplicates keep both of their sign-ins working: the guard only fires when the attempted method has no account yet for that email.

Added marketing attribution for account creations and app downloads:

- New accounts are stamped with the `imbue_attribution` marketing cookie's first/last touch and anonymous visitor id (plus the signup page's own campaign params) at creation time, on both the email-password and Google OAuth signup paths. Sign-ins of existing accounts never record anything, and capture fails open so it can never break signup.

- New `GET /download` endpoint records a campaign-tagged download event and 302s to the stable per-platform installer link (`platform=mac-arm64`/`mac` for the macOS build, `platform=source` for the public GitHub repo; unknown platforms 404). Campaign params on the URL itself tag cookie-less downloads, and the redirect always happens even if the event write fails.

- New Neon tables `account_attribution` and `download_events` (migration 026); reporting is plain SQL, joined exactly via the cookie's visitor id.

- The cookie's contract with the imbue.com marketing site (schema, set/update rules, and download/signup link formats) is pinned in `docs/attribution-cookie-contract.md`.

- The OAuth `AuthResponse` gained an additive `is_new_account` field (True when the exchange created the account), threaded from SuperTokens' `created_new_recipe_user`.

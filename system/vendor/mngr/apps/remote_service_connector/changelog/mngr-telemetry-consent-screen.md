The hosted signup page now offers a plan choice and requires a terms agreement:

- A plan selector (Explorer preselected) whose options carry the workspace counts -- "Explorer (2 free cloud workspaces)" / "Free (1 free cloud workspace)" -- with a short per-plan description and a "Learn more" link to the privacy policy. The chosen plan's entitlements row is created at account creation on both the password and Google paths (the choice rides the OAuth state JWT); the write fails open to the lazy backfill.

- Signing in or up on the hosted pages with no pending destination now lands on the web client (/web) instead of the account page (/manage); flows with an explicit next -- the desktop app's login handoff, share visits -- are unchanged.

- A single agreement checkbox (unchecked by default) linking the Terms of Service and Code of Conduct. Both creation paths are gated client-side with a clear error; a Google exchange that would create an account without the agreement (the sign-in tab's button) is rolled back server-side and bounced to a terms_required banner.

- New static HTML placeholder pages served from the accounts bundle: /terms-of-service, /code-of-conduct, and /privacy-policy.

- A new "free" plan (1 remote workspace, no in-workspace analytics collection) joins explorer and ally. The lazy entitlements backfill now assigns "free" instead of "explorer" for accounts without a recorded signup choice, since explorer-plan membership is the analytics-collection consent and must only ever come from an explicit user choice (pre-cutoff paid-listed accounts still backfill as ally).

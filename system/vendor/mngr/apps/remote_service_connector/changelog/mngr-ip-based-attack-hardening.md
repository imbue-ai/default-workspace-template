# IP-based signup hardening (velocity limits + IP reputation, graduated step-up)

- Account creation on the hosted accounts surface is now gated on the client IP (issue mngr-internal#467), on the tiers whose signup is restricted to that surface (production/staging). Per-IP (hourly) and per-subnet (/24 v4, /48 v6, daily) velocity caps answer `RATE_LIMITED`; Tor-exit and hosting/datacenter IPs are blocked outright (`SIGNUP_BLOCKED`), and vpn/proxy/relay IPs (residential proxies included) are stepped up to OAuth-only (`OAUTH_ONLY`): the password form is refused while Continue with Google still works. A Google-created account refused by the gate is rolled back before any session is minted. Returning sign-ins are never gated.

- IP reputation comes from the IPinfo Max lookup API (new `IPINFO_TOKEN` key in the supertokens secret; lookups cached per IP in the new `ip_reputation_cache` table and budget-capped per day), unioned with a free hourly-refreshed Tor-exit-list check that works with no token. Everything fails open -- a Neon, IPinfo, or Tor-list outage degrades signup to "Turnstile plus whatever signal remains" -- keeping Turnstile the only fail-closed gate.

- Every gated attempt (allowed ones included) is recorded in the new `signup_attempts` table (migration 028) with its IP, subnet, verdict, and outcome, and logged, so a signup flood is visible in real time instead of being reconstructed from Modal logs afterwards. Dev/CI tiers record verdicts but never refuse.

- The client IP used for the gate (and for Turnstile's advisory `remoteip`) is now derived exclusively from the ASGI socket peer -- verified as Modal's trustworthy channel -- never from `X-Forwarded-For` or any other forwarding-style header.

- The hosted login page shows the new `signup_blocked` banner for refused Google signups, and an `OAUTH_ONLY` refusal re-collapses the email/password fields so Continue with Google is the visible path.

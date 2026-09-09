Add the bug-investigation skill suite under `.claude/skills/`: `investigate-bug` plus the four `access-*` skills it routes to.

`investigate-bug` is the routing hub: classify a user-facing minds failure from its error text (client-side vs. server-reached), route to the observability system that actually holds the evidence, probe live connector health, correlate across systems (deploy id, environment, Bugsink event id, and the two disjoint identity spaces), and interpret absence correctly (positive controls, the consent gate on automatic-vs-manual Sentry events, retention windows).

`access-sentry` covers hosted Sentry.io (desktop-client errors + user bug reports) through latchkey: runtime discovery of org/projects, the validated endpoint set (org-scoped issues, Discover body-level search, full-event fetch), field-tested search gotchas, and the bug-report channel including the S3 attachment pointers.

`access-bugsink` and `access-openobserve` cover the self-hosted per-tier stores (server-side exceptions; server-side request logs/metrics): the SSH-tunnel transport with per-session Vault key fetch, the per-tier local-port and latchkey-service conventions, validated API recipes (Bugsink canonical issues API; OpenObserve SQL `_search` with `spath`), and personal-credential minting/rotation.

`access-analytics` is a deliberate stub pointing at the DuckLake attach docs; analytics is for aggregate questions, not incidents.

All five skills follow the one-sentence-per-line prose standard and keep private coordinates (IPs, org slugs, Vault addresses) out of the text.

The skills declare themselves living documents, with a Linear-ticket protocol for correcting stale information.

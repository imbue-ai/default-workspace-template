# Remove flip_feature_flags.sh

Its two flags -- `FEATURE_FLAG_ENABLE_OTHER_HARNESSES` and
`FEATURE_FLAG_ENABLE_INTRODUCTORY_AGENTS_IN_OTHER_HARNESSES` -- are gone, and they were the
only ones. Both gated which tiles the new-tab screen offered; the provider picker carries
that choice now, as a real user-facing one rather than a host-side toggle.

supervisord.conf's flag block goes with it. There are no feature flags left in this
workspace; which harnesses a user can launch is decided by which providers they have
signed in to.

`migrate_claude_auth.py` now migrates into an account rather than into the shared
settings.json that accounts replaced, and loses its whole detached-restart half: an
account is read when a chat is created, not frozen into a running process's environment,
so nothing has to be torn down to see it. 179 lines to 88.

Bump the pinned Antigravity CLI from 1.1.16 to 1.1.22 (`system/scripts/agy_install-1.1.22.sh`,
URL + per-arch sha512 re-captured from the live manifest while current).

Stop pinning a codex model. `gpt-5.6-sol` was set on `agent_types.codex`, but which models a
ChatGPT plan may use varies by tier, so one account's default is another's hard 400 -- and
because the model bar matches the live model against the account's own list, an unusable pin
also renders as a shrug that hides every model the account CAN use. Codex now picks its
account's default, like pi, opencode and antigravity already did. Claude keeps its pin and is
now the only agent type with one.

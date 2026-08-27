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

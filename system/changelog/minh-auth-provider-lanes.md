# Remove flip_feature_flags.sh

Its two flags -- `FEATURE_FLAG_ENABLE_OTHER_HARNESSES` and
`FEATURE_FLAG_ENABLE_INTRODUCTORY_AGENTS_IN_OTHER_HARNESSES` -- are gone, and they were the
only ones. Both gated which tiles the new-tab screen offered; the provider picker carries
that choice now, as a real user-facing one rather than a host-side toggle.

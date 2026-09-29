Two tests that need a non-root user carry the new `may_skip_in_workspace` marker, since everything in a workspace runs as root.

Removed `test_handoff_release.py` and `test_message_conservation_release.py`. Both needed live Claude (and OpenRouter) credentials, which no CI run has ever had, so only a deliberate manual run could exercise them and none was scheduled.

The suite now also runs inside the workspace image on Modal in CI, where its `browser` tests really run (any skipped test fails the job there, unless marked `may_skip_in_workspace`). Its coverage is measured there, mapped back to the checkout (`[tool.coverage.paths]`) and held to the same 75% floor on the runner.

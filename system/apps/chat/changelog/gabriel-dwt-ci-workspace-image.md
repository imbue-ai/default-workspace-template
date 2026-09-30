Filing or submitting a secret request against an existing env file that is not UTF-8 text now answers with an error naming the file, where it used to fail with an unhandled decode error.

Two tests that skipped as root (and everything in a workspace runs as root) now provoke their failure in a way that holds for any user, so they run in a workspace too.

Removed `test_handoff_release.py` and `test_message_conservation_release.py`. Both needed live Claude (and OpenRouter) credentials, which no CI run has ever had, so only a deliberate manual run could exercise them and none was scheduled.

The suite now also runs inside the workspace image on Modal in CI, where its `browser` tests really run (any skipped test fails the job there). Its coverage is measured there, mapped back to the checkout (`[tool.coverage.paths]`) and held to the same 75% floor on the runner.

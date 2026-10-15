The ledger module's docstring says the launch-task script now imports it from the root venv (a declared dependency) rather than through a `sys.path` edit.

The new `oom_priority.supervisord_testing` module holds `supervisord_commands`, the supervisord program reader the band tests, the app-manifest tests and the supervisord layout test share.

The launch-wrapper and memory-candidates tests import their scripts like the other `bin/` tests instead of loading them by path. Now that the type checker sees `memory_candidates`'s real types, the tests annotate their `Sources` helper and check that the candidate lists are present before reading them.

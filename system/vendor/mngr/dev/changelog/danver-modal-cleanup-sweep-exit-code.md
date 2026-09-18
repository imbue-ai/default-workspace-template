dev: `scripts/cleanup_old_modal_test_environments.py` now exits non-zero when an environment survives its delete.

The script's `main()` returned 0 unconditionally, and the sweep it calls never raises. The `cleanup-modal-environments` CI job therefore reported success whether it had deleted everything, nothing, or hit a wall of permission errors -- a green check there was not evidence that any cleanup happened. That is how the wrong-JSON-key bug fixed in the previous PR went unnoticed for six weeks.

The script now prints which environments were left behind and exits 1, so a sweep that cannot reap turns the job red. A run with nothing to do, or one where every environment is gone (deleted by us or already absent), still exits 0.

The script also gained `--dry-run`, which prints the environments the sweep would touch and deletes nothing. It selects them through the same `find_old_test_environments` the real sweep uses and simply stops before acting, so a rehearsal cannot disagree with the run it is rehearsing.

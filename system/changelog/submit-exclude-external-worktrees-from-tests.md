Running the test suite from the repo root no longer breaks when you have a standalone clone of another repo checked out under `.external_worktrees/`. That directory is gitignored and holds other projects' code, but neither pytest nor the meta ratchets skipped it, so both treated whatever was in there as part of this repo.

For pytest this was fatal rather than noisy. Collection imports every `*_test.py` it finds, and top-level code in an imported file runs during collection -- so a scratch script that calls `sys.exit()` when it cannot find an API key ended the whole session with an `INTERNALERROR` before a single test of ours ran. The failure named the external file, but nothing about it suggested the fix was a collection setting rather than something broken in this repo.

For the meta ratchets it was noise with the same effect: 94 shell scripts from an unrelated project counted as violations of this repo's bash strict-mode rule, failing a check that nobody working here could act on.

Both now skip the directory, alongside the exclusions they already had for vendored code, the workspace data directory, and virtualenvs.

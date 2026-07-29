- `assert_mngr_argv_valid` now also checks every `-S KEY=VALUE` config override an
  argv carries, by running it through mngr's own `apply_settings_to_config`. click
  treats a `-S` value as an opaque string, so before this an argv could carry a key
  path that mngr rejects at startup -- taking the whole command down -- and still
  pass the contract check.

- The overrides are read back off click's own parse rather than re-scanned out of
  the argv, so every spelling click accepts (`-S K=V`, `-SK=V`, `--setting=K=V`) is
  covered without this module re-deriving any of them.

- This tightens validation for every existing caller of the contract, not just new
  ones, so it ships on its own rather than inside a feature branch.

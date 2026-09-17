- CI's pytest steps no longer set `PYTEST_MAX_DURATION_SECONDS`: nothing in this repo reads
  it now that the apps no longer register mngr-internal's conftest hooks. The root
  `pyproject.toml` declares the `flaky` marker beside `acceptance` and `release`.

- `AGENTS.md` no longer tells agents to set `PYTEST_MAX_DURATION_SECONDS` to their tool timeout,
  and the per-harness notes in `.mngr/settings.toml` that pointed at that line are gone: the
  variable fed the hooks' global test lock, which no longer exists here. `AGENTS.md` also stops saying pytest writes slow-test and coverage
  reports under `.test_output/`; that was the hooks' `--slow-tests-to-file` / `--coverage-to-file`,
  and those reports now print at the end of each app run.

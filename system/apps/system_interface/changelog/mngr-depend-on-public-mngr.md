- The frontend build reads the embed contract and the service icons from
  `system/vendor/mngr-assets/` (fetched from the pinned mngr commit by the
  `prebuild` step) instead of a vendored mngr tree. `update_staleness.py` no
  longer treats a vendored mngr path specially.

- The app's `conftest.py` is gone: it only registered mngr-internal's shared pytest hooks
  (`imbue.imbue_common.conftest_hooks` -- a global test lock, a suite duration cap, test
  profiles keyed to a file this repo does not have), copied over when this tree was a copy
  of the monorepo. The `acceptance` / `release` / `flaky` markers those hooks declared are
  now declared in this app's `pyproject.toml`; the hook-only `--slow-tests-to-file` /
  `--coverage-to-file` options and the `resource-guards` test dependency go with it.

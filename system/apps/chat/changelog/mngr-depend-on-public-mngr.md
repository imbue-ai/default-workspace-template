- The chat frontend bundles the embed contract and the service icons from
  `system/vendor/mngr-assets/` (fetched from the pinned mngr commit by the npm
  workspace root's `prebuild`/`pretest`) instead of a vendored mngr tree.

- The app's `conftest.py` is gone: it only registered mngr-internal's shared pytest hooks
  (`imbue.imbue_common.conftest_hooks`), copied over when this tree was a copy of the
  monorepo. The `acceptance` / `release` / `flaky` markers those hooks declared are now
  declared in this app's `pyproject.toml`; the hook-only `--slow-tests-to-file` /
  `--coverage-to-file` options and the `resource-guards` test dependency go with it.

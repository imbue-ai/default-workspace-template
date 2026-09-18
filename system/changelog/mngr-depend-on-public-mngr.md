- mngr is no longer vendored. `system/vendor/mngr/` (a copy of the private
  mngr-internal monorepo) is gone; mngr and its plugins are installed as Python
  packages from the public mngr repo, https://github.com/imbue-ai/mngr, at the
  one commit `pyproject.toml` pins under `[tool.uv.sources]`.
  `system/scripts/build_workspace.sh` derives the `mngr` tool
  (`system/scripts/install_mngr.py`), each app's tool, and the workspace venv
  from that pin; `system/config/mngr_plugins.toml` lists each plugin's package
  and repo subdirectory.

- The few files the workspace needs from mngr that no published package carries
  (the embed contract and service icons the UI bundles, the style guide) are
  fetched at build time by `system/scripts/fetch_mngr_assets.sh` into gitignored
  `system/vendor/mngr-assets/`.

- There is no way to build a workspace against any other mngr: the build reads
  the pin and nothing else. A workspace built by the mngr repo's dev loop or CI
  harnesses runs the pinned mngr too.

- `system/test_mngr_pin.py` pins the shape: public repo, full commit, every locked
  mngr package at the pin, nothing tracked under `system/vendor/mngr`.
  `system/test_supervisord_layout.py`'s evals-capture release gate, which could
  only read the capture from a vendored tree, is gone; mngr-internal's own
  `evidence_collection_test.py` covers that capture.

- `system/scripts/pull_upstreams.sh` and `push_upstreams.sh` are gone: they existed to
  split vendored-mngr edits from workspace edits, and there is no vendored mngr to
  split. mngr changes are their own PR on the mngr repo.

- The root dev group depends on `imbue-common[testing]` (the extra that declares what
  `imbue_common.ratchet_testing` needs) instead of naming `import-linter` itself.

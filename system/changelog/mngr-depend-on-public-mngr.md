- mngr is no longer vendored. `system/vendor/mngr/` (a copy of the private
  mngr-internal monorepo) is gone; mngr and its plugins are installed as Python
  packages from the public mngr repo, https://github.com/imbue-ai/mngr, at the
  one commit `pyproject.toml` pins under `[tool.uv.sources]`.
  `system/scripts/build_workspace.sh` derives the `mngr` and `system-interface`
  tools (`system/scripts/build_mngr_tools.sh`, one `uv tool install` each so
  uv resolves the whole set at one commit) and the workspace venv from that pin;
  `system/config/mngr_plugins.toml` lists each plugin's package and repo
  subdirectory.

- The few non-Python files the workspace needs from mngr (the embed contract and
  service icons the UI bundles, the terminal's ttyd client) are fetched at build
  time by `system/scripts/fetch_mngr_assets.sh` into gitignored
  `system/vendor/mngr-assets/`.

- An mngr checkout dropped (untracked) at `system/vendor/mngr/` takes over the
  pin: `system/scripts/use_local_mngr.py` runs first in the build and rewrites
  the mngr sources to editable paths into the tree. This is how the mngr repo's
  dev loop and CI harnesses run a checkout's mngr in a workspace. Nothing tracked
  changes; deleting the tree restores the pin.

- `system/test_mngr_pin.py` pins the shape: public repo, full commit, every locked
  mngr package at the pin, nothing tracked under `system/vendor/mngr`.

- `pull_upstreams.sh` / `push_upstreams.sh` refuse the mngr leg; mngr changes are
  their own PR on the mngr repo.

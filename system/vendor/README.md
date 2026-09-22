# system/vendor/

- `mngr-assets/` - Gitignored. The few non-Python files this workspace needs from
  [mngr](https://github.com/imbue-ai/mngr), fetched at build time from the commit
  `pyproject.toml` pins (`system/scripts/fetch_mngr_assets.sh`). mngr itself is
  installed as Python packages from that same commit.
- `tk/` - A vendored copy of the [tk](https://github.com/wedow/ticket) ticket
  tracker the agents use for task management (tickets live in `data/.tickets/`).

# system/

The machinery that runs this workspace. Users don't need anything in here
day-to-day, but every part is inspectable and the mind maintains it.

- `libs/` - The built-in workspace packages: background services, the
  workspace web UI, and shared libraries.
- `scripts/` - Provisioning and utility scripts (image build, boot, hooks,
  service helpers).
- `vendor/` - Vendored external repos: `mngr` (the agent manager this
  workspace runs on) and `tk` (the ticket tracker).
- `config/` - Tracked workspace configuration (`parent.toml`, the upstream
  template pointer). Runtime-written config lives in `data/system/` instead.
- `changelog/` - Per-change entries for template development.
- `Dockerfile` - Builds the workspace image.
- `supervisord.conf` - Defines the background services (also reachable at
  `/etc/supervisord.conf`, so `supervisorctl` works from any directory).
- `test_meta_ratchets.py`, `test_mngr_template_stacking.py` - Repo-wide test
  suites.

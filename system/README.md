# system/

The machinery that runs this workspace. Users don't need anything in here
day-to-day, but every part is inspectable and the mind maintains it.

- `apps/` - Everything you can open as a tab: the built-in apps and the apps
  your mind builds for you.
- `services/` - Standalone background services (supervised or cron-driven).
- `libs/` - Support libraries, including the first-boot bootstrap and the
  automations machinery.
- `scripts/` - Provisioning and utility scripts (image build, boot, Claude
  Code hooks).
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

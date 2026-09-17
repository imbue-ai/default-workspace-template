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
- `supervisord.conf` - The supervisord daemon's own config, plus the
  `[include]` glob that pulls in `supervisord.conf.d/` (also reachable at
  `/etc/supervisord.conf`, so `supervisorctl` works from any directory).
- `supervisord.conf.d/` - One `[program:*]` per file: every background service
  and app. Add a service by writing a file here, remove one by deleting it.
- `test_meta_ratchets.py`, `test_mngr_template_stacking.py` - Repo-wide test
  suites.

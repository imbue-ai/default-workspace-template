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
- `supervisord.conf` - Daemon config, the `system_interface` program, and an
  `[include]` of `supervisord.conf.d/*.conf` (also reachable at
  `/etc/supervisord.conf`, so `supervisorctl` works from any directory).
- `supervisord.conf.d/` - One file per background service, named
  `<service-name>.conf`. Adding or removing a service is adding or deleting a
  file here, so concurrent service work never collides on a shared config.
- `test_meta_ratchets.py`, `test_mngr_template_stacking.py` - Repo-wide test
  suites.

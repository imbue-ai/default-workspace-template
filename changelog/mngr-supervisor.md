- Replaced the custom bootstrap "service manager" with **supervisord**.
  Background services are now defined as `[program:*]` sections in a versioned
  `supervisord.conf` at the repo root (the old `services.toml` and the
  tmux-window-per-service reconcile/watch loop are gone). `uv run bootstrap`
  still runs first-boot setup and then `exec`s `supervisord -n` in the
  foreground from the `bootstrap` extra_window. `supervisor` is installed
  system-wide via `scripts/setup_system.sh` (covering every provider, including
  lima). Edit `supervisord.conf` and run `supervisorctl reread && supervisorctl
  update` to apply service changes.

- The `system-services` agent now runs a real (idle) Claude agent in window 0
  instead of `sleep infinity`: the `[agent_types.main]` command override was
  removed (so it falls back to the default `claude`), and a background-services
  system prompt is appended via `[create_templates.main]` `agent_args`. It is
  told it will be sent operational errors to triage -- fixing directly or
  delegating to a worker via `launch-task`.

- Cleaned up the `[create_templates.main]` `extra_window` list: `bootstrap` is
  now the only entry. `telegram` was retired, the `git_auth_setup` commands now
  run inside `bootstrap` (minus the obsolete `gh auth setup-git`), `terminal`
  (ttyd) became a supervisord service, and `deferred-install` became a one-shot
  supervisord program.

- Service logs are now separate, rotated, container-local files under
  `/var/log/supervisor/<name>-stdout.log` / `<name>-stderr.log` (not under
  `runtime/`, so they are not backed up).

- Updated the `edit-services` and `build-web-service` skills (and the
  `scaffold_fastapi_lib.py` scaffolder) to emit supervisord `[program:*]` blocks
  and use `supervisorctl`, plus refreshed `CLAUDE.md`, `README.md`,
  `libs/bootstrap/`, `libs/web_server`, and several other skills/scripts that
  referenced the old `services.toml` / `svc-<name>` model.

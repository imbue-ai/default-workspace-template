Background services now live one-per-file under `system/supervisord.conf.d/`, pulled in by an `[include]` glob in `system/supervisord.conf`. Adding a service is writing `system/supervisord.conf.d/<name>.conf`; removing one is deleting that file.

This exists so two agents can build two independent services in the same workspace at the same time. Previously every new service appended a `[program:*]` block to the single shared `system/supervisord.conf` and added entries to the root `pyproject.toml`, so one agent's in-progress service sat as uncommitted changes in the other's way -- and the background hardening pass, which needs a clean tree to hand a worker, could not run.

`system_interface` deliberately stays in the main `system/supervisord.conf`: the minds desktop client's recovery probe parses that file directly with `configparser`, which does not follow supervisord's `[include]`, so moving it would silently break the probe's port and health checks.

Start order is unchanged. supervisord orders programs by `(priority, name)`, so which file a program is declared in has no effect on when it starts.

The `build-web-service` scaffolder writes the new service's program to its own drop-in and no longer edits the root `pyproject.toml` at all -- the `creations/*` uv workspace member glob already covers the package and `uv sync --all-packages` installs it, so the dependency and `[tool.uv.sources]` entries were redundant. Its port pre-flight scans the drop-ins too, and it refuses a name already claimed by any existing program.

Scaffolded services now start with `uv run --all-packages <name>` instead of a bare `uv run`. A plain `uv sync` prunes every workspace member the root does not depend on -- which is now all of `creations/` -- and `--all-packages` reinstates them, so a service repairs its own environment on restart.

`github-sync` writes its own drop-in when enabled rather than appending to the shared config, `update-self` classifies `system/supervisord.conf.d/**` as a service-class path, and the service-teardown, contention, and service-process references follow the new layout.

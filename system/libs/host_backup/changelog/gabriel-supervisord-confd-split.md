Background services now live one-per-file under `system/supervisord.conf.d/`, pulled in by an `[include]` glob in `system/supervisord.conf`. Adding a service is writing `system/supervisord.conf.d/<name>.conf`; removing one is deleting that file.

This exists so two agents can build two independent services in the same workspace at the same time. Previously every new service appended a `[program:*]` block to the single shared `system/supervisord.conf` and added entries to the root `pyproject.toml`, so one agent's in-progress service sat as uncommitted changes in the other's way -- and the background hardening pass, which needs a clean tree to hand a worker, could not run.

`system_interface` deliberately stays in the main `system/supervisord.conf`: the minds desktop client's recovery probe parses that file directly with `configparser`, which does not follow supervisord's `[include]`, so moving it would silently break the probe's port and health checks.

Start order is unchanged. supervisord orders programs by `(priority, name)`, so which file a program is declared in has no effect on when it starts.

For this project: the `[program:host-backup]` block moves to `system/supervisord.conf.d/host-backup.conf`. Its stable contract with the minds backup-service updater is intact -- minds only ever reaches this service as `supervisorctl status|restart host-backup`, by program name, and the name, the root `pyproject.toml` registration, and the `uv run host-backup` entry point are all unchanged.

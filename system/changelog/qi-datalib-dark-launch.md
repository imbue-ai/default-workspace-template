datalib ships in every workspace, unannounced and off by default, so it can be tried without adopting the separate datalib template. Nothing of it is downloaded or started until the `datalib` skill is first used.

- `system/supervisord.conf.d/datalib.conf`: the `datalib` program that runs the Datalib app (see `system/apps/datalib/`), with `autostart=false`.

- `pyproject.toml`: `system/apps/datalib` is excluded from the uv workspace, like the file viewer.

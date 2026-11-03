# app_registration

Writes the app registry, `data/.state/apps.toml`: one `[[apps]]` row per app, with
its URL, icon and the static fields of its `app.toml`. `forward_port` upserts or
removes a row under the registry's lock and validates what it copies (the name
rule, the icon markup, the value types); its module docstring describes the rules.

## Consumers

- `system/scripts/forward_port.py` -- the command every supervisord program line
  runs before its own command, and that Imbue Studio and minds_evals run.
- `app_manifest.registry.register_app` -- an app's entry point registering itself.
- `system/apps/datalib/launch_datalib_http.py` -- Datalib's launcher.
- The isolated-instance, preview and scaffold code under `.agents/`.

The stub and Datalib's launcher import this package under a bare `python3` (no
virtualenv), via an explicit `sys.path` entry, which is why it is **stdlib-only**
and must stay that way. Reading the registry is `app_manifest.registry`'s job.

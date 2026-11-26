# Removing an app

The teardown steps for an app. What to leave behind (the app's data) is
SKILL.md's rule, under "Removing an app or service".

1. If the app is still framed as a mock, take the frame down first, which closes
   the mock window and removes the `<name>-sketch` app:
   `uv run --no-sync .agents/skills/build-app/python/build_app_skill/mock_frame.py down --app <name>`.
2. Remove the app:

   ```bash
   uv run --no-sync app-install uninstall <name>
   ```

   It stops and removes the app's programs, and removes its generated files
   (its program files under `system/supervisord.conf.d/` and any
   `.agents/skills/` link for a skill it carried), its registry row in
   `data/.state/apps.toml`, its directory `system/apps/<package>/`, and its
   record `system/config/apps/<name>.toml`, then commits the removal. Do not
   delete any of these by hand.

   The command ends in one JSON line. Its `problems` list is empty when
   everything was removed; anything it names (supervisord or the shell could
   not be reached, for example) is what is left for you to do.
3. The app's stored data and its secret values are kept, and the result lists
   them: `data/.apps/<name>/` and each `data/.secrets/<file>.env` the app
   declared. Ask the user before deleting either.

The app is a standalone project with its own `uv.lock` and venv, so nothing
shared needs editing: the root `pyproject.toml` and `uv.lock` never named it,
and the app's venv directory beside the uv cache is deleted by the next run of
the venv-link helper (at boot, worktree setup or an update) once its `.venv`
link is gone.

An app with no record under `system/config/apps/` is not one `app-install`
removes. Take it down as a service ("Removing a service" in
`.agents/shared/references/service-processes.md`, which also drops its
`data/.state/apps.toml` entry), then `rm -rf system/apps/<package>/`.

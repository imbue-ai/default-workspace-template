# Removing an app

The teardown steps for an app, the same for one installed from someone else's
repository as for one built here. What to leave behind (the app's data) is
SKILL.md's rule, under "Removing an app or service".

1. If the app is still framed as a mock, take the frame down first, which closes
   the mock window and removes the `<name>-sketch` app:
   `uv run --no-sync .agents/skills/build-app/python/build_app_skill/mock_frame.py down --app <name>`.
2. Find out what uses the app before it goes, since removing it is not held
   back by anything that depends on it. `uv run --no-sync app-install list`
   shows the paths the app's record lists under `references`: the skills,
   scripts and docs elsewhere in the workspace that use it. Another app that
   expects this one lists it under `[[dependencies]]` in its own manifest
   (`grep -l 'name = "<name>"' system/apps/*/app.toml` names this app's
   manifest and every such app). Tell the user what will stop working before
   you go on.
3. Remove the app:

   ```bash
   uv run --no-sync app-install uninstall <name>
   ```

   It stops and removes the app's programs, and removes its generated files
   (its program files under `system/supervisord.conf.d/` and any
   `.agents/skills/` link for a skill it carried), its registry row in
   `data/.state/apps.toml`, its directory `system/apps/<package>/`, and its
   record `system/config/apps/<name>.toml`, then commits the removal. An
   update or a publish of the app that was waiting part-way goes with it. Do
   not delete any of these by hand.

   The command ends in one JSON line. `removed_paths` is what it took out of
   the tree, and `removed_state_paths` the machine state that went with it.
   Its `problems` list is empty when everything was removed; anything it
   names (supervisord or the shell could not be reached, for example) is what
   is left for you to do.
4. The rest of the result is what was left on purpose. Tell the user about
   each part that is not empty, and delete or change one only when they ask:
   - `kept_paths`: the app's stored data (`data/.apps/<name>/`), each secret
     file it declared (`data/.secrets/<file>.env`), and, for an installed
     app, the copy of its data that its last update made
     (`data/.state/app_install/snapshots/<name>/`).
   - `kept_share_grants`: the people the app was shared with (`grant_count`
     of them, in the `table` of the file at `path`). Grants are kept by the
     app's name, so an app of the same name added later is shared with the
     same people. Say that plainly; it matters to someone who removes an app
     to stop sharing it.
   - `kept_routine_state`: what each of the app's routines kept (the on/off
     switch, a schedule the user gave it, its run history), by the routine's
     id, so a later routine of that id takes it over.
   - `kept_repositories`: the app's repositories on GitHub (its private dev
     repository and, when it was published, its public one), which nothing
     here touches. People who installed the app from the public one keep it.
   - `dependents`: the apps still in the workspace that list this one under
     `[[dependencies]]`. They were not removed and may no longer work.

The app is a standalone project with its own `uv.lock` and venv, so nothing
shared needs editing: the root `pyproject.toml` and `uv.lock` never named it,
and the app's venv directory beside the uv cache is deleted by the next run of
the venv-link helper (at boot, worktree setup or an update) once its `.venv`
link is gone.

An app with no record under `system/config/apps/` is not one `app-install`
removes. Take it down as a service ("Removing a service" in
`.agents/shared/references/service-processes.md`, which also drops its
`data/.state/apps.toml` entry), then `rm -rf system/apps/<package>/`.

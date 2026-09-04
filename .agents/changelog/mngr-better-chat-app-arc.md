Phase 1 of the workspace app model (`docs/system/blueprint/workspace-app-model/`):

- The build-app scaffold writes an `app.toml` manifest for a new app (name, display name from the new `--display-name` or the description, icon, `instances = false`, `priority = "user"`, `program`), no longer edits the root `pyproject.toml`, installs the app as its own uv tool (`uv tool install -e system/apps/<package>`) before `uv sync --all-packages`, and writes a supervisord line that registers with `forward_port.py --manifest ... --url ...` and runs the app's own entry point instead of `uv run <name>`. The skill doc, the update-app live loop (reinstall the tool after a dependency change), the cleanup, verify and cross-flow-gotchas references, and the shared service-processes reference describe the manifest-driven line.

- The manifest is the discriminator for the new environment model: the update-self apply treats an app as a tool only when its directory has both a `pyproject.toml` and an `app.toml`; an app scaffolded before manifests existed keeps running `uv run <name>` from the root venv, untouched, until phase 9's migration rewrites it.

- The update-self apply refreshes tool environments per app: any change under `system/apps/<package>/` (outside `frontend/` and `static/`) reinstalls that app's tool from the merged tree with the plugins `system/config/mngr_plugins.toml` assigns to its manifest name, the tool directory of every `critical` app the diff touched is copied aside before the apply and restored on rollback, and a non-critical app's tool is reinstalled from the restored tree instead. A backend manifest change (the root or a vendored `pyproject.toml`, `uv.lock`, the plugin table) still refreshes the mngr tool and the root venv, and now reinstalls every app's tool as well, since those manifests are part of each tool's closure.

- `serve_isolated_instance.py` invokes `forward_port.py` under a plain `python3` now that the script is standard-library only.

- The migrate-workspace port scan (`migrate_workspace.py list-ports`) reads the manifest-form program lines too: a `forward_port.py --manifest ... --url ...` call, which carries no `--name`, is reported under its `[program:<name>]`, so the built-ins and every newly scaffolded app still count toward name and port collisions on both sides of a migration.

Phase 3 of the workspace app model (the terminal app):

- The migrate-workspace port scan (`migrate_workspace.py list-ports`) also reports a registry row's `instances_url` port beside its `url` port, so an app serving its instances API on a second port (the terminal's 7682) counts toward name and port collisions.

- The update-self apply's tests know the terminal as a tool: `system/apps/terminal` has a `pyproject.toml` and an `app.toml` now, so a change under it reinstalls `terminal-app`, and it is `critical` (snapshot-and-rollback).

Phase 4 of the workspace app model (the files app):

- The update-self apply's tests know the files app as a tool: `system/apps/files` has a `pyproject.toml` beside its `app.toml` now, so a change under it (the vendored `assets/` included: they are not one of the excluded directories, and an editable reinstall is harmless) reinstalls `files-app`, and it is not `critical`.

- The migrate-workspace port scan's test of the real `system/supervisord.conf` expects the files app, like the terminal, to register from inside its own process (`files-app`), so the config itself names ports only for the shell and the browser; the registry scan covers 8300 and 8301 through the row's `url` and `instances_url`.

Phase 6 of the workspace app model (chat as a document):

- The migrate-workspace port scan (`migrate_workspace.py list-ports`) reports one row for a program that registers two manifests at one port: the shell's line now registers the chat app's manifest beside its own at port 8000, and the registry scan is what names the `chat` row.

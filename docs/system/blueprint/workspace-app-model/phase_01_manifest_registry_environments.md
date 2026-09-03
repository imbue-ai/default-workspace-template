# Phase 1: manifest, registry, and environments

Contracts: [contracts.md](contracts.md) sections 2, 3, 14, and 15.
Nothing user-visible changes in this phase; every app registers with the same name, icon, and origin it has today.

## Goal

Give every built-in app a manifest, teach the registration script to read it, extend the registry rows, install every manifest app as its own tool environment (the manifest is the discriminator; a pre-manifest user app keeps `uv run` until phase 9), and switch the memory backstop to the registry's `priority`.

## Files

Created:

- `system/libs/app_manifest/pyproject.toml`, `README.md`, `changelog/mngr-better-chat-app-arc.md`, `src/app_manifest/__init__.py`, `src/app_manifest/manifest.py`, `src/app_manifest/registry.py`, `src/app_manifest/cli.py`, `test_app_manifest_ratchets.py`, and unit tests beside each module.
  - `manifest.py`: `AppManifest` (FrozenModel, `extra = "forbid"`) with the fields of contracts section 2, `AppAction`, `DefaultShortcut`, `load_manifest(path) -> AppManifest`, and the error type `AppManifestError`.
  - `registry.py`: `RegistryRow` (the row shape of contracts section 3 with defaults for absent keys), `read_registry(path) -> list[RegistryRow]` that logs and skips rows failing validation, and `registry_path()` honouring `MINDS_APPS_FILE`.
  - `cli.py`: `validate-manifest <path>` for the scaffold and tests.
- `system/apps/system_interface/app.toml`, `system/apps/terminal/app.toml`, `system/apps/terminal/icon.svg`, `system/apps/files/app.toml`, `system/apps/browser/app.toml`, with the values in contracts section 2.
  The chat manifest is created in phase 6.
- `system/scripts/forward_port_test.py` gains cases for `--manifest`; the existing cases stay.

Modified:

- `system/scripts/forward_port.py`: drops `tomlkit` for `tomllib` plus the private writer of contracts section 3; gains `--manifest`; copies the manifest fields onto the row; keeps every existing flag.
- `system/supervisord.conf`: every app's registration line passes `--manifest system/apps/<package>/app.toml --url ...` instead of `--name`, `--icon-file`, `--program`; every Python app's program runs its tool entry point (`system-interface`, `browser-service`) with no `uv run`.
- `system/scripts/build_workspace.sh`: replaces the single `system-interface` tool install with the loop of contracts section 14 and reads each app's plugin list from the plugin table by app name.
- `system/config/mngr_plugins.toml`: the `tools` lists name apps by manifest name; `system-interface` keeps its five harness plugins until phase 10 moves them to `chat`.
- `system/scripts/list_mngr_plugins.py`: `--tool` accepts any name in the table.
- `pyproject.toml` (root): `browser` leaves `[project.dependencies]`; `app-manifest` joins `[tool.uv.sources]`. `system/apps/*` stays in the workspace member glob for now (contracts section 14): an existing workspace's root pyproject names its scaffolded apps as `{ workspace = true }` sources, so removing the glob before those apps are migrated would break its next `uv sync --all-packages --frozen` and roll the update back. Phase 9 removes the glob together with the pre-manifest apps.
- `.agents/skills/update-self/scripts/update_environment.py` and `update_classification.py`: the environment refresh reinstalls the tool of every changed app directory and snapshots the tool directories of `critical` apps; the classification treats `system/apps/<package>/**` outside `frontend/` and `static/` as that app's environment.
- `.agents/skills/build-app/scripts/scaffold_flask_lib.py` and `SKILL.md`, `.agents/skills/update-app/SKILL.md`, `.agents/shared/references/service-processes.md`: the scaffold writes an `app.toml`, installs the new app as a tool, and writes the manifest-driven registration line; the update-app live loop reinstalls the tool after a dependency change.
- `system/services/oom_priority/src/oom_priority/bands.py`: adds `"chat": 25`; `supervisord_program_band` takes the registry rows as an argument and resolves through `priority`.
- `system/services/oom_priority/bin/oom_tag_backstop.py`: reads the registry (stdlib `tomllib`, honouring `MINDS_APPS_FILE`) on each event and passes the rows in.
- `system/services/oom_priority/README.md`, `system/apps/README.md`, `system/libs/README.md`, `docs/system/workspace-internals.md`: describe manifests, tool environments, and `priority`.

Deleted: nothing.

## Behaviour

- Registration with `--manifest` is authoritative for every manifest field on every call, like `--internal` today; a manifest-less registration leaves the new keys absent.
- The shell keeps its own registry reader in this phase (phase 7 replaces it with `app_manifest.registry`, where a row that fails validation is skipped with a warning naming the file and the field, and the app is absent from every surface until fixed).
- The build script, the scaffold, and the apply treat an app as a tool only when its directory has both a `pyproject.toml` and an `app.toml`; a pre-manifest user app is left exactly as it is.
- The backstop's lookup is by `program`; every built-in row carries its program, so the existing `test_every_built_in_supervisord_program_has_an_explicit_band` requirement becomes "every built-in manifest declares a `priority` that is a band key".
- `uv tool install` runs from the repository root so uv discovers the workspace and resolves the path dependencies (`app_manifest`, `oom-priority`, `imbue-common`, the vendored mngr packages) editable.

## Tests

- `app_manifest`: every field's default and rule, `actions` forbidden with `instances = false`, `default_shortcut.action` must exist, `instances_url` shape, registry rows with absent keys, a bad row skipped and logged.
- `forward_port_test.py`: `--manifest` copies every field; a manifest whose `name` differs from `--name` is refused; the writer round-trips a row with an SVG icon containing quotes and newlines through `tomllib`; a manifest-less registration writes exactly today's keys.
- `oom_priority`: `supervisord_program_band` resolves `priority` through rows; a program with no row falls back to `USER_SERVICE`; `chat` is between `system_interface` and `share-gateway`.
- `system/test_mngr_template_stacking.py` and `toolchain_pins_test.py` keep passing; a new `system/test_app_manifests.py` loads every `system/apps/*/app.toml` and asserts it validates, that its `program` exists in `supervisord.conf`, and that its registration line names it.
- `build_workspace.sh` is exercised by the Dockerfile build in CI as today.

## Manual verification

In a dev workspace (`just minds-start`): `uv tool list` shows one tool per Python app; `cat data/.state/apps.toml` shows `display_name`, `instances`, `critical`, and `priority` on every built-in row; the rail, the launcher, and minds' servers page look exactly as before; `supervisorctl status` is all `RUNNING`; `cat /proc/<pid>/oom_score_adj` for the shell and the browser daemon matches their bands.

## Changelog entries

`system/libs/app_manifest/changelog/mngr-better-chat-app-arc.md` (new project), `system/apps/system_interface/changelog/mngr-better-chat-app-arc.md`, `system/apps/browser/changelog/mngr-better-chat-app-arc.md`, `system/services/oom_priority/changelog/mngr-better-chat-app-arc.md`, `.agents/changelog/mngr-better-chat-app-arc.md`, and the existing `system/changelog/mngr-better-chat-app-arc.md` (scripts, supervisord, docs).
One entry per project for the whole PR; each phase appends to its projects' entries.

## Exit criteria

Every test above passes, the Docker image builds, and a fresh workspace boots with every app registered and running from its own tool.

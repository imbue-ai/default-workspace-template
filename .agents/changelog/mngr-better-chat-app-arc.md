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

Phase 7 of the workspace app model (the shell core):

- Every skill that drives the workspace layout speaks addresses: `manage-layout` and `manage-projects` are rewritten around `app:<name>` and `app:<name>?instance=<key>`, `--view`, the relay verbs, and the shortcut subcommands; the caretaker, migrate-workspace, manage-scheduled-tasks, and update-self docs and `update_self.py` (`surface-chat-tab` takes `--agent-id` and opens `app:chat?instance=$MNGR_AGENT_ID`) follow.

- update-self's teardown closes a stale preview tab before deregistering its app (an op addressed to an unregistered app is refused); build-app describes `layout.py open` as a no-op for an already-open tab and `list` as the per-app instance listing.

- The build-app scaffold's index page posts `{type: "shell:location", path}` (the contract's message) rather than the retired `minds-location`, which the shell no longer accepts; the skill doc describes the beacon as relayed to the app's own instances API.


Phase 8 of the workspace app model (the layout file is the truth):

- `manage-layout` describes the new model: every op targets exactly one client (`--client`), ops land with no browser connected and answer at once, `--view` edits a view and switches the client to it, `open` takes `--action` and `--param` and a bare URL, and the exit codes (a 3 is now the app refusing for now, not a mutex).

- `update-system-interface`'s `reveal_system_interface.py` probes the shell's `/api/health` rather than the chat's `/api/agents`.

Phase 9 of the workspace app model (the migration):

- The update-self apply runs `system/scripts/migrate_workspace_layouts.py run` from the merged tree after the pre-flight and before the services restart, so the restarted shell reads a pre-app-model workspace's migrated projects and layouts at once. A failure (or a script that cannot be spawned) is a warning, never a rollback: the migration never overwrites an output that holds anything (the app stores only gain records), leaves the old store untouched, and runs again at the next boot.

- The comments in the apply's classification and layout modules no longer promise that a migration rewrites pre-manifest apps: an app scaffolded before manifests keeps running `uv run <name>` from the root venv for as long as it exists, beside the manifest apps' tool environments.

Phase 10 of the workspace app model (the chat app): the update apply's health probe polls the shell's `/api/health` (no longer the chat's `/api/agents`) and then the chat app's own `/api/health`, at the URL the registry (`data/.state/apps.toml`, row `chat`) holds or `http://127.0.0.1:8010` when the registry has no row; a chat that does not come back healthy after the restart is an apply failure like the shell's. The apply plan refreshes the `chat` tool when the chat package, a shared backend manifest, the vendored mngr, or the plugin table changes.

The update apply builds the frontends at the npm workspace root (`system/`), snapshots and restores `node_modules` there, snapshots and verifies both bundles (the shell's `static/index.html` and the chat's `static/chat.html`, each stamped with the tree hashes of its frontend directory, the shared library, and the lockfile), and classifies a change under either frontend, the library, the shared tooling, or any of the workspace's npm manifests as frontend work. `update_self.py apply --worker-bundle` takes `<app>=<path>` once per app (`system_interface=...`, `chat=...`); the pair is installed only when both are given and verified, and a live build is the fallback. The `update-self` and `update-system-interface` skills say so.

The apply's chat probe says on stderr when the app registry cannot be read and it falls back to the default chat URL, and the worker briefs (`update-self-worker.md`, `type-system-interface.md`) build both frontends at the npm root and report both bundle paths.

The apply's chat probe treats a registry chat row that names the shell's own origin as the pre-split registration and probes the default chat URL instead (a workspace updating into the split still carries that row until the chat re-registers, and the shell answered for it); a rollback probes the chat as well where the restored tree runs it as its own program; the stale-bundle note prints its stamps on one line; and the worker briefs and the `update-system-interface` skill describe the two-process model (the chat project to validate, the npm-root gate, the freshness paths over the shared library and the lockfile).

A rollback into a tree from before the chat's split (the tree a failed first update to this release restores) recovers: the apply reads the restored tree's frontend layout, rebuilds only the bundles that tree serves and only when their copies could not be put back, and runs npm at the root that tree has (the shell's own frontend directory when there is no `system/package.json`) instead of failing at a root the tree does not have and exiting as an emergency over a restored shell bundle.

The apply's chat health failure (and the recovery's) names the URL it probed; the `update-system-interface` freshness check covers `system/apps/chat/frontend/` and its exit-3 guidance names both bundle copies.

A rollback into a tree from before the chat's split removes the chat bundle the forward build wrote (that tree neither tracks nor ignores `system/apps/chat/imbue/chat/static/`, so it kept the tree dirty and every later apply refused) on the boot path (`recover --no-restart`) as well as the live one, and its recovery rebuild at the shell's own frontend reinstalls that frontend's `node_modules` first (the forward `npm ci` at the workspace root had emptied it). The `update-self` skill's editing-lease rule covers every tree that rebuilds the shell's bundle (the chat frontend, the shared library, the npm manifests); `update-app` and `assist` describe the shell without the chat panels; `build-app` lists the chat app's port 8010 among the ports to avoid.

The update apply pre-flights the merged chat app beside the shell: `chat-app --preflight` boots on a free port and must answer `/api/health` before the live services are restarted, so a broken plugin table, a missing dependency in the chat's tool environment, or an import error in harness code is refused up front (with the boot's output) instead of surfacing in the post-restart probe and its rollback. A tree from before the chat's split has no chat program and skips the check.

Phase 11 of the workspace app model: after the restart the update apply probes the shell's `/api/health` and then the instances API (`GET /_instances`) of every app whose manifest in the merged tree says `critical = true` and `instances = true` (the chat and the terminal), each at its manifest's `instances_url` when it declares one, else at the registry row's `url` re-read on every poll, so the probe follows an app that re-registers at the end of its boot; a missing row is "not up yet", and only a 200 with a JSON body counts (a stale row that still names the shell's port gets the shell's SPA catch-all, 200 as HTML). This replaces the chat's `/api/health` probe, which answered while the chat's agent manager was stuck, the hard-coded chat port, and the shell-origin guard; the rollback path holds the restored tree's apps to the same probe. The chat's pre-flight boot keeps `/api/health`. No `supervisorctl reread && update` step was added: the apply's restart of the services agent brings up a fresh supervisord that reads the merged program table, so a program an update adds starts on its own; `update-self/SKILL.md` says so.

A tool the update adds (the chat, terminal, and files apps, for a workspace from before the app model) is installed beside the mngr tool rather than left to uv's default tool directory: that default follows `$HOME`, which at runtime is not the directory `build_workspace.sh` installed under and whose bin directory is on nobody's PATH, so the new tool installed fine and was never found (the chat pre-flight's `chat-app: not found` rolled the first real pre-arc upgrade back). The refresh says where it put such a tool.

The update-self worker reference and the apply-outcomes reference say what a retry after a rolled-back apply must do first: revert the `Roll back update apply` commit on the worker's branch before merging the target again. The rollback is a forward revert, so git counts the target's content as already merged and a plain re-merge lands only what the target gained since the failed attempt, a tree the apply's probes cannot tell from a good update (seen on the first real pre-arc upgrade: the second pass applied the old release plus three files and reported success).

The preview wrapper page's comment (`.agents/shared/scripts/preview_wrapper_server.py`) points at `deriveAppOrigin` in `system/libs/workspace_ui/src/origin.ts`, the function and module that own the origin scheme it mirrors, rather than at the old name in the shell's own frontend.

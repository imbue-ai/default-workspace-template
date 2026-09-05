Add the workspace app model meta spec (`docs/system/blueprint/workspace-app-model/plan-workspace-app-model.md`): the workspace as an operating system of web apps, app-owned instances with app-scoped keys and shifting URLs, a manifest-driven registry, the app contract (instances API, browser-side postMessage protocol, location and embedder relays, shared instances library with a sidecar launcher for wrapped servers), one uv tool environment per Python program, client-scoped layouts over shared projects, the chat app's extraction from the system interface (chat pages become a separate document before the package moves; every tab is an instance, including new-chat and subagent pages), the layout migration, and the eleven commit-ordered phases of the single implementing pull request. It replaces the split-chat-apart plan that lived in the mngr repo. The folder also holds `contracts.md` (every schema, route, message, and file format), one spec per phase (`phase_01_*.md` to `phase_11_*.md`), and `mngr_side_changes.md` for the paired mngr branch.

Phase 1 of that model, "manifest, registry, and environments" (nothing user-visible changes: every app registers with the same name, icon, and origin):

- Every built-in app ships a manifest, `system/apps/<package>/app.toml` (`system_interface`, `terminal`, `files`, `browser`), with the values of `contracts.md` section 2; the terminal and browser gain an `icon.svg` matching the glyph the rail already draws.

- `system/scripts/forward_port.py` is standard-library only (`tomllib` to read, a private writer for the registry's flat `[[apps]]` shape, so registration never depends on the root venv) and gains `--manifest <path>`, which reads an app's manifest, validates its name and icon, and copies `display_name`, `instances`, `instances_url`, `critical`, `priority`, `program`, `internal`, `default_shortcut`, and `actions` onto the registry row, authoritatively on every call. `--name --url`, `--internal`, `--no-icon`, `--icon-file`, `--program`, and `--remove` keep working for manifest-less registrations.

- `system/supervisord.conf` and `system/apps/terminal/run_ttyd.sh` register every built-in app through its manifest, and every Python app's program runs its tool entry point (`system-interface`, `browser-service`) with no `uv run`.

- `system/scripts/build_workspace.sh` installs one uv tool per manifest app (every `system/apps/<package>/` with both a `pyproject.toml` and an `app.toml`; an app scaffolded before manifests existed keeps `uv run` from the root venv until the phase 9 migration rewrites it), each with the mngr plugins `system/config/mngr_plugins.toml` assigns to its manifest name (the table's `tools` lists now name apps by manifest name, so `system-interface` became `system_interface`; `list_mngr_plugins.py --tool` accepts any name in the table). The apps stay uv workspace members so one lockfile covers the tree and existing workspaces' root pyprojects keep resolving; `browser` leaves the root `[project.dependencies]`.

- New `system/test_app_manifests.py`: every built-in manifest validates, names a real supervisord program that registers with it, and declares a `priority` that is a memory band.

- The new `app_manifest` library (`system/libs/app_manifest`), the `chat` memory band and the registry-driven backstop in `oom_priority`, and the READMEs (`system/apps`, `system/libs`, `system/services/oom_priority`, `docs/system/workspace-internals.md`) describe the model.

Phase 2 of that model, "the instances library" (nothing user-visible changes: nothing mounts the blueprint yet):

- New `system/libs/app_instances` (see its own changelog): the instances API blueprint, the JSON store, the shell nudge, and the sidecar launcher for wrapped third-party servers; `system/libs/README.md` lists it.

- `contracts.md` section 4 gained the rules the library enforces: an instance URL or path is rooted with a single slash and carries no control characters, a title is non-blank and at most 256 characters, and a key that fails the key rule answers `400` on every keyed route (including `DELETE`, which otherwise answers `204` for any key).

Phase 3 of that model, "the terminal app" (nothing user-visible changes: the same tabs, titles, session switching, renaming, and reattach after a container restart):

- `system/supervisord.conf`'s `[program:terminal]` runs `terminal-app`, the new Python package under `system/apps/terminal` (see its own changelog), instead of `bash system/apps/terminal/run_ttyd.sh`, which is deleted; the root `pyproject.toml` no longer excludes the terminal from the uv workspace, so `uv.lock` gained the package.

- `system/test_app_manifests.py` accepts a program that registers from inside its own entry point (the console script's module exports `MANIFEST_PATH`), and `system/apps/README.md` and `system/services/app_watcher/README.md` describe the terminal as a package.

- `contracts.md` section 4.3's terminal URL gained the leading `arg=_` (it lands in `$0` of the `bash -c` dispatch, as today's frontend sends it) and the optional trailing `workdir` argument, and its title column names the stored title; `phase_03_terminal_app.md` records what landed, including the terminal's own store (the library's `JsonStoreInstanceSource` cannot hold a renamed name or a `{tab}` URL) and the naming rule renames follow.

Phase 4 of that model, "the files app" (nothing user-visible changes: the file viewer opens, browses, and reopens where it was, exactly as before):

- `system/supervisord.conf`'s `[program:files]` runs `files-app`, the new Python package under `system/apps/files` (see its own changelog), instead of the `bash -c` line that registered the manifest and exec'd dufs; the root `pyproject.toml` no longer excludes the files app from the uv workspace (the `exclude` key is gone), so `uv.lock` gained the package.

- `contracts.md` gained section 17, where app data and machine state live: every app's instance records at `data/.apps/<name>/instances.json`, machine state (the registry, the terminal's dispatch scripts and pty records, the shell's client layouts) under `data/.state/`; the root `CLAUDE.md` and `AGENTS.md` sentences on `data/` point at it, and the terminal's store moved from `data/.state/terminal/` to `data/.apps/terminal/instances.json` accordingly (see the terminal's changelog). `phase_04_files_app.md` records what landed; `system/apps/README.md` describes the files app as a package.

Phase 5 of that model, "the browser app" (nothing user-visible changes: the browser tab, the fleet CLI, and the shell's browser routes behave as before):

- The browser daemon serves the instances API over its fleet (see the browser's changelog), so `curl http://127.0.0.1:8081/_instances` lists the open browsers with `working`, `idle`, or `error` status; the root `uv.lock` gained the browser's `app-instances` and `app-manifest` dependencies.

- `contracts.md` section 4.2 lets a location report carry an absolute `http(s)` URL for an app that navigates to other sites' pages, each app taking the form that fits it and answering `400` for the other, and section 4.3's browser row says what the browser does with one (navigates the active tab; `409` while an agent holds the browser or while it is launching or crashed) and lists a launching browser as `idle`. `phase_05_browser_app.md` records what landed.

- The browser's fleet manifest moved from `data/.state/browser-fleet.json` to `data/.apps/browser/instances.json` per contracts section 17 (`data/.state/README.md` says so); the old file is read until the daemon first writes the new one.

Phase 6 of that model, "chat as a document" (nothing user-visible changes: every chat renders inside an iframe at its own `chat` origin, served by the system-interface process, and behaves as before):

- `system/apps/chat/` holds the chat app's manifest (`app.toml`, `contracts.md` section 2's chat row: `instances = true`, `critical = true`, `default_shortcut` `new` in new mode, actions `new` and `subagent`) and its icon; no code yet, which runs inside `system/apps/system_interface` until phase 10, so the root `pyproject.toml` excludes the directory from the uv workspace's member glob and `system/test_app_manifests.py` checks the manifest against the shell's program line. Until phase 7 and 10 the manifest also says `internal = true`, `program = "system_interface"`, and `priority = "system_interface"` (each a `# CLEANUP:`), so the shell offers no second Chat row and the memory backstop keeps the shell's process in its own band.

- `system/supervisord.conf`'s `[program:system_interface]` registers the chat manifest beside the shell's, at the shell's port (`forward_port.py --manifest system/apps/chat/app.toml --url http://localhost:8000`), so the chat origin exists whenever the shell does and a preview or pre-flight boot of the shell never re-points it.

- The shell serves the browser-side contract module of `contracts.md` section 10 at `GET /_static/app_contract.js` (CORS `*`) and the probe route `GET /api/health` of section 5; the system interface's own changelog describes the split.

- `contracts.md` records what phase 6 settled: the chat manifest's shared-process values above; the chat row's title for a provisional instance (its minted display name) and the `subagent` action's optional `description`; `shell:open`'s optional `title` hint, which phase 7 drops. `phase_06_chat_as_document.md` records what landed (requests dispatched to the chat app by path rather than by origin label, since the desktop client's forwarder rewrites the `Host` header; the readiness gate; `shell:open` for chat addresses in this phase; the provider chooser staying on the shell until phase 10); `phase_07_shell_core.md`, `phase_10_chat_app.md`, and `mngr_side_changes.md` carry the follow-ons.

Phase 7 of that model, "the shell core" (the shell is generic over apps and their instances; see the system interface's changelog):

- `system/scripts/layout.py` speaks addresses: `app:<name>` and `app:<name>?instance=<key>`, a bare word as `app:<word>`; the old spellings (`chat:`, `terminal:`, `service:`, `url:`, `subagent:`, `chat-terminal:`) are refused with an error naming the new form, and an external URL with an error naming phase 8. `open` of a bare app with instances creates a fresh instance and prints its address to stdout; `rename`, `delete`, and `replace-url` go through the shell's relay to the app; `shortcuts`, `shortcut set <app> <action> --mode`, and `shortcut remove` configure a project's rail. `--view` names the view (`--layout` stays as an alias).

- `system/apps/chat/app.toml` drops `internal = true` (the shell lists apps from the inventory now); `system/test_app_manifests.py` checks that. `contracts.md` records the two carve-outs that outlive this phase (`agents_updated` and the proto-agent messages until phase 10, `load_layout` until phase 8) and which halves of section 12 landed; `phase_07_shell_core.md` records what landed and the ten decisions taken on the way.

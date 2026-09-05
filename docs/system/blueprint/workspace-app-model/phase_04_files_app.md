# Phase 4: the files app

Contracts: [contracts.md](contracts.md) sections 4.3 (files row), 5, 10, and 17.
User-visible behaviour is unchanged: the file viewer opens, browses, and reopens where it was, exactly as before.

## Goal

Make the files app an unchanged dufs plus an instances sidecar, so two file browsers on different folders reopen where they were.

## Files

Created, under `system/apps/files/`:

- `pyproject.toml` (name `files-app`; depends on `app-instances`, `app-manifest`, `click`, `imbue-common`, `pydantic`), `changelog/mngr-better-chat-app-arc.md`, `test_files_ratchets.py`, `conftest.py` (the `files_environment` fixture, one call to `app_instances.testing.prepare_sidecar_environment`).
- `src/files_app/main.py`: the entry point `files-app`, a click command whose options (`--manifest`, `--app-url`, `--instances-url`, `--store`, `--dufs`) default to the fixed wiring (`MANIFEST_PATH = system/apps/files/app.toml`, the app URL `http://localhost:8300`, the instances URL `http://127.0.0.1:8301`, the store `data/.apps/files/instances.json` from the library's `app_store_path`, the `dufs` binary), parsed into a `FilesAppArguments` FrozenModel.
  `run_files_app` calls the library's `run_sidecar` with the exact `dufs --allow-all --bind 127.0.0.1 --port 8300 --assets system/apps/files/assets data` line from the old supervisord program (`build_dufs_argv`; the port comes from the app URL through the library's new `app_url_port`) and a `JsonStoreInstanceSource` with prefix `files`, title template `File Viewer {n}`, lifetime `referenced`, not renameable, location tracking on (`build_files_source`).
  `MANIFEST_PATH` is what `system/test_app_manifests.py` imports to check the registration.
- `src/files_app/testing.py`: a fake `dufs` (a bash script recording its argv, then sleeping) for the process-level test.
- `src/files_app/main_test.py`: the wiring test (the manifest agrees with the constants, the source's prefix, template, lifetime, renameable and location flags, the dufs argv as an inline snapshot); `test_files_app.py`: runs `python -m files_app.main` as a process around the fake dufs on scratch ports and checks the registry row, the dufs argv, the empty list, two creates (`files-1` at the given path, `files-2` at `/`), the delete that frees `files-1` and the create that reuses it, a location report landing in the list and the store, a `400` rename, and exit 143 on `SIGTERM`.

Modified:

- `system/apps/files/assets/index.js`: the beacon's message type is `shell:location`; the `?v=minds-N` revision in `index.html` is `minds-3` (all three asset references); nothing else in the vendored frontend changes.
- `system/supervisord.conf`: `[program:files]` runs `files-app` (under the `oom_tag_service.py files` prefix, as before).
- `system/apps/system_interface/frontend/src/locationBeacon.ts`: accepts `shell:location` beside `minds-location` until phase 7 replaces it (`// CLEANUP:`); `locationBeacon.test.ts` asserts both are recorded.
- `system/apps/files/README.md`, `system/apps/README.md`, the shell README's beacon sentence, the `app_instances` README.
- `pyproject.toml` (root): files leaves the workspace `exclude` list (the key is gone); `uv.lock` re-locked.
- `system/libs/app_instances/sidecar.py`: `app_url_port(app_url)`, the wrapped server's port read from the app URL, shared with the terminal (which drops its own `ttyd_port`).
- `system/test_app_manifests.py`: with `files-app` as the program, the manifest is found through `MANIFEST_PATH`, as for the terminal; a stale comment saying files has no Python is gone.
- `.agents/skills/update-self/scripts/update_self_test.py`: the files app is a tool the apply refreshes (not critical); a change under `assets/` reinstalls it too, since only `frontend/` and `static/` are excluded and an editable reinstall is harmless.
- `.agents/skills/migrate-workspace/scripts/migrate_workspace_test.py`: the real `supervisord.conf` names ports for the shell and the browser only; the terminal and files register from inside their processes and the registry scan covers their ports.

Done at the start of this phase, as its own commit: contracts section 17 (where app data and machine state live), the root `CLAUDE.md` sentence pointing at it, and the terminal's store moved from `data/.state/terminal/instances.json` to `data/.apps/terminal/instances.json` (`terminal-app --store`), so every app keeps its instance records under `data/.apps/<name>/` and `data/.state/terminal/` holds only the dispatch scripts and pty records.
`AGENTS.md`'s copy of that sentence was aligned with `CLAUDE.md`'s later in the phase; its other differences from `CLAUDE.md` are prior work and were left alone.

## Behaviour

- Until phase 7 the shell still stores locations in its own store and still mints `files-<N>` through its allocator; both keep working because the dufs origin and the beacon shape are unchanged apart from the type string, and the shell's beacon listener accepts both `minds-location` and `shell:location` from this phase on.
  `# CLEANUP:` the old type and the shell's store go in phase 7; the migration in phase 9 imports the store into the sidecar's JSON file.
- From this phase both the shell's allocator and the sidecar's store exist, and only the sidecar's is reachable through `/_instances`; the sidecar's list is authoritative from phase 7 on: an instance is a key plus the path it was last at.
- The `build-app` scaffold keeps telling a user-built app to post `minds-location`: the spec scopes the rename to the dufs beacon, a single-instance app has no location route for the relay to reach after phase 7 anyway, and phase 11's skill rewrites move the scaffold onto `app_contract.js`.

## Tests

- Wiring test and the process-level test as above; the library's own tests cover the store and the sidecar (`app_url_port` gained a case in `sidecar_test.py`).
- The frontend unit test in the shell asserts the beacon listener accepts both type strings (removed in phase 7).

## Manual verification

`curl -X POST http://127.0.0.1:8301/_instances -d '{"action":"new","params":{"path":"/data/docs/"}}'` returns `files-1` with that url; a second create returns `files-2`; deleting `files-1` and creating again reuses `files-1`.
Done at the end of the phase as a local smoke run of `files-app` around the real pinned dufs binary on scratch ports; the live-container check (`just minds-start`) was deferred, as in phase 3.

## Changelog entries

`system/apps/files/changelog/mngr-better-chat-app-arc.md` (new project), `system/changelog/mngr-better-chat-app-arc.md`, `system/apps/terminal/changelog/mngr-better-chat-app-arc.md`, `system/libs/app_instances/changelog/mngr-better-chat-app-arc.md`, `system/apps/system_interface/changelog/mngr-better-chat-app-arc.md`, and `.agents/changelog/mngr-better-chat-app-arc.md`.

## Exit criteria

dufs serves as before at its origin, and the sidecar lists, creates, deletes, and records locations for instances.

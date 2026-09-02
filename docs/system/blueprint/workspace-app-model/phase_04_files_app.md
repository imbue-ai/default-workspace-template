# Phase 4: the files app

Contracts: [contracts.md](contracts.md) sections 4.3 (files row), 5, and 10.

## Goal

Make the files app an unchanged dufs plus an instances sidecar, so two file browsers on different folders reopen where they were.

## Files

Created, under `system/apps/files/`:

- `pyproject.toml` (depends on `app-instances`, `app-manifest`), `changelog/mngr-better-chat-app-arc.md`, `test_files_ratchets.py`.
- `src/files_app/__init__.py`, `src/files_app/main.py`: the entry point `files-app`, calling `run_sidecar` with dufs as the child (the exact `dufs --allow-all --bind 127.0.0.1 --port 8300 --assets system/apps/files/assets data` line from today's supervisord), the app URL `http://localhost:8300`, the instances URL `http://127.0.0.1:8301`, and a `JsonStoreInstanceSource` with prefix `files`, title template `File Viewer {n}`, lifetime `referenced`, and location tracking on.
- A unit test for the wiring (the store's prefix, lifetime, and title).

Modified:

- `system/apps/files/assets/index.js`: the beacon's message type becomes `shell:location` and the `?v=minds-N` revision in `index.html` is bumped; nothing else in the vendored frontend changes.
- `system/supervisord.conf`: `[program:files]` runs `files-app`.
- `system/apps/system_interface/frontend/src/locationBeacon.ts`: accepts `shell:location` beside `minds-location` until phase 7 replaces it.
- `system/apps/files/README.md`.
- `pyproject.toml` (root): files leaves the workspace `exclude` list.

## Behaviour

- Until phase 7 the shell still stores locations in its own store and still mints `files-<N>` through its allocator; both keep working because the dufs origin and the beacon shape are unchanged apart from the type string, and the shell's beacon listener accepts both `minds-location` and `shell:location` from this phase on.
  `# CLEANUP:` the old type and the shell's store go in phase 7; the migration in phase 9 imports the store into the sidecar's JSON file.
- The sidecar's list is authoritative from phase 7 on: an instance is a key plus the path it was last at.

## Tests

- Wiring test as above; the library's own tests cover the store and the sidecar.
- A frontend unit test in the shell asserts the beacon listener accepts both type strings (removed in phase 7).

## Manual verification

`curl -X POST http://127.0.0.1:8301/_instances -d '{"action":"new","params":{"path":"/data/docs/"}}'` returns `files-1` with that url; a second create returns `files-2`; deleting `files-1` and creating again reuses `files-1`.

## Changelog entries

`system/apps/files/changelog/mngr-better-chat-app-arc.md` (new project), `system/changelog/mngr-better-chat-app-arc.md`.

## Exit criteria

dufs serves as before at its origin, and the sidecar lists, creates, deletes, and records locations for instances.

# Phase 2: the instances library

Contracts: [contracts.md](contracts.md) sections 1, 4, 5, and 14.
Nothing user-visible changes in this phase.

## Goal

Build `system/libs/app_instances`: the blueprint every multi-instance app serves, the JSON store, the nudge, and the sidecar launcher that wraps a third-party server.

## Files

Created, all under `system/libs/app_instances/`:

- `pyproject.toml` (depends on `flask`, `httpx`, `app-manifest`, `imbue-common`), `README.md`, `changelog/mngr-better-chat-app-arc.md`, `test_app_instances_ratchets.py`.
- `src/app_instances/primitives.py`: the validated strings `InstanceKey`, `InstanceKeyPrefix`, `InstanceUrl`, `LocationPath`, `InstanceTitle`, and `TitleTemplate` (the rules of contracts 1 and 4.1); and, from phase 3, the workspace's naming rule as the shell applies it to chats, `canonical_name_from_title` ("My Build" to "My-Build") and `is_name_conflict` (canonical forms compared case-insensitively), for every app whose keys are true names of user-typed titles.
- `src/app_instances/data_types.py`: `InstanceStatus` and `InstanceLifetime` enums, `InstanceRecord` (FrozenModel of contracts 4.1, `last_active` anchored to UTC), `CreateRequest`, `RenameRequest`, `LocationRequest`.
- `src/app_instances/interfaces.py`: `InstanceSourceInterface` (MutableModel, ABC) with `list_instances()`, `create_instance(action, params)`, `delete_instance(key)`, `rename_instance(key, title)`, `set_location(key, path)`, each raising the typed errors below; `InstanceNudgerInterface` with `nudge()`.
- `src/app_instances/errors.py`: `AppInstancesError` and `UnknownActionError`, `InvalidParamsError`, `UnknownInstanceError`, `NotRenameableError`, `LocationNotTrackedError`, `InstanceConflictError`, `NotReadyError`, each mapped to the status code in contracts 4.2 by the blueprint (`InvalidInstanceValueError` for a value that breaks a primitive's rule is `400`; `InstanceStoreError` and `SidecarError` are the store's and the launcher's own failures, and any unmapped library error answers `500` with a detail body).
- `src/app_instances/blueprint.py`: `build_instances_blueprint(source, nudger)`; every mutating route calls the source, then `nudger.nudge()`, then answers. `build_instances_app(source, nudger)` is a Flask app serving nothing but the blueprint, which the sidecar and the stub app run. From phase 3, its body parsing and its error answer are public (`parse_request_body(model)`, raising `MalformedRequestError`, and `answer_typed_error`, the `AppInstancesError` handler) for an app whose own routes sit beside the blueprint.
- `src/app_instances/json_store.py`: `JsonStoreInstanceSource`, records under `data/.apps/<name>/instances.json` (`app_store_path(name)`; `{"version": 1, "instances": [record, ...]}`), written atomically under the source's own lock (one source per process must be the file's only writer), with `allocate_key(prefix, taken_keys)` minting the lowest free `<prefix>-<N>`; `create_instance` for action `new` stores `params.path` (default `/`) as the url and refuses any other param; `set_location` records the path and refreshes `last_active`; `key_prefix`, `title_template`, `lifetime`, `is_renameable`, and `is_location_tracked` are constructor fields. An unreadable or malformed store raises rather than reading as empty, so a corrupt file is a loud `500`, never a silent loss of every record. The file handling is public as `read_json_document(path, model)` and `write_json_document(path, document)` (phase 3), for an app whose instances have backing state of their own and so keeps a document shape of its own.
- `src/app_instances/nudge.py`: `ShellNudger` posting `POST <shell>/api/apps/<name>/changed` with a two-second timeout, swallowing connection errors at debug level; `<shell>` resolves as `layout.py` does.
- `src/app_instances/sidecar.py`: `run_sidecar(manifest_path, app_url, instances_url, child_argv, source)` (a wrapper over `run_sidecar_app(..., build_app)`, added in phase 3 for an app that mounts routes of its own beside the blueprint; `build_app(manifest, nudger)` returns the Flask app to serve): starts the blueprint on the `instances_url` port on a daemon thread (werkzeug threaded server), then registers through `forward_port.py --manifest` (run under the current interpreter, from the repo root), spawns the child with `subprocess.Popen`, forwards `SIGTERM` and `SIGINT` to it, and returns the exit status the app's entry point ends with: the child's code, or 128 plus the signal number when a signal killed it; `stopasgroup` in supervisord covers the rest. The manifest must declare `instances = true` with an `instances_url` equal to the one served, and the call must run on the main thread.
- `src/app_instances/testing.py`: `StubInstanceSource` (in-memory, records every call), `RecordingNudger`, `free_port`, `wait_until`, `write_sidecar_manifest`, and `run_stub_app(port)` for the shell's tests in later phases; as a module it serves the stub app or runs the sidecar over a JSON store, which is how the integration test drives the sidecar as a real process.
- Unit tests beside each module, and `test_sidecar.py` (integration) that wraps `python3 -m http.server` as a child and checks registration, the instances routes, and shutdown.

## Behaviour

- The blueprint serves exactly the routes of contracts 4.2 and nothing else; an app mounts it on its own Flask app or the sidecar serves it alone.
- The blueprint validates keys against contracts section 1 before calling the source.
- `NotReadyError` from `list_instances` answers `503`; the shell then keeps the last known list.
- The sidecar's registration runs after the blueprint is listening, so the shell's first fetch succeeds.

## Tests

- Blueprint: each route's success and each error mapping, nudge called after every mutation and not after reads, key validation.
- JSON store: allocation fills gaps, atomic write survives a crash mid-write (temp file left behind is ignored), `set_location` rejects unrooted or over-long paths, records survive a reload.
- Sidecar integration: registration row present with `instances_url`, child reachable at the app URL, `SIGTERM` reaches the child, exit code propagates.

## Manual verification

`uv run python -m app_instances.testing stub --port <port>` serves the stub app on a free port; `curl` lists, creates, renames, and deletes an instance and the nudge shows up in the shell log as an unknown app `404` (the shell learns the route in phase 7).

## Changelog entries

`system/libs/app_instances/changelog/mngr-better-chat-app-arc.md` (new project), `system/libs/README.md` under `system/changelog/`.

## Exit criteria

The library's tests pass with coverage, and a scratch app built on it lists, creates, and deletes instances through both the mounted blueprint and the sidecar.

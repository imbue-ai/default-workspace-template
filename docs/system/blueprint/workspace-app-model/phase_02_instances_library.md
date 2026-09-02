# Phase 2: the instances library

Contracts: [contracts.md](contracts.md) sections 1, 4, 5, and 14.
Nothing user-visible changes in this phase.

## Goal

Build `system/libs/app_instances`: the blueprint every multi-instance app serves, the JSON store, the nudge, and the sidecar launcher that wraps a third-party server.

## Files

Created, all under `system/libs/app_instances/`:

- `pyproject.toml` (depends on `flask`, `httpx`, `app-manifest`, `imbue-common`), `README.md`, `changelog/mngr-better-chat-app-arc.md`, `test_app_instances_ratchets.py`.
- `src/app_instances/data_types.py`: `InstanceStatus` and `InstanceLifetime` enums, `InstanceKey` (validated string), `InstanceRecord` (FrozenModel of contracts 4.1), `CreateRequest`, `RenameRequest`, `LocationRequest`.
- `src/app_instances/interfaces.py`: `InstanceSourceInterface` (MutableModel, ABC) with `list_instances()`, `create_instance(action, params)`, `delete_instance(key)`, `rename_instance(key, title)`, `set_location(key, path)`, each raising the typed errors below; `InstanceNudgerInterface` with `nudge()`.
- `src/app_instances/errors.py`: `AppInstancesError` and `UnknownActionError`, `UnknownInstanceError`, `NotRenameableError`, `LocationNotTrackedError`, `InstanceConflictError`, `NotReadyError`, each mapped to the status code in contracts 4.2 by the blueprint.
- `src/app_instances/blueprint.py`: `build_instances_blueprint(source, nudger)`; every mutating route calls the source, then `nudger.nudge()`, then answers.
- `src/app_instances/json_store.py`: `JsonStoreInstanceSource`, records under `data/.apps/<name>/instances.json` (`{"version": 1, "instances": [record, ...]}`), written atomically under a lock, with `allocate_key(prefix)` minting the lowest free `<prefix>-<N>`; `create_instance` for action `new` stores `params.path` (default `/`) as the url; `set_location` records the path; `lifetime` and `title` template are constructor arguments.
- `src/app_instances/nudge.py`: `ShellNudger` posting `POST <shell>/api/apps/<name>/changed` with a two-second timeout, swallowing connection errors at debug level; `<shell>` resolves as `layout.py` does.
- `src/app_instances/sidecar.py`: `run_sidecar(manifest_path, app_url, instances_url, child_argv, source)`: registers through `forward_port.py --manifest`, starts the blueprint on the `instances_url` port on a daemon thread (werkzeug threaded server), spawns the child with `subprocess.Popen`, forwards `SIGTERM` to it, and exits with the child's code; `stopasgroup` in supervisord covers the rest.
- `src/app_instances/testing.py`: `StubInstanceSource` (in-memory, records every call) and `run_stub_app()` for the shell's tests in later phases.
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

`uv run python -m app_instances.testing` serves the stub app on a free port; `curl` lists, creates, renames, and deletes an instance and the nudge shows up in the shell log as an unknown app `404` (the shell learns the route in phase 7).

## Changelog entries

`system/libs/app_instances/changelog/mngr-better-chat-app-arc.md` (new project), `system/libs/README.md` under `system/changelog/`.

## Exit criteria

The library's tests pass with coverage, and a scratch app built on it lists, creates, and deletes instances through both the mounted blueprint and the sidecar.

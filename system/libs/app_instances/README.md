# app_instances

The shared implementation of the instances API every multi-instance workspace
app serves (`contracts.md` sections 4 and 5 of the workspace app model, in
`docs/system/blueprint/workspace-app-model/`): the Flask blueprint over a
pluggable instance source, a JSON store for apps whose instances have no other
backing state, the nudge that tells the shell a list changed, and a sidecar
launcher that wraps a third-party server. A user who wants two dashboards side
by side pays nothing new: mount the blueprint over a source, or let the sidecar
serve it beside the wrapped server.

The shell is the only caller of the API, over loopback, at the registry row's
`instances_url`. Browsers never reach it; the shell relays every instance verb.

## API

- `app_instances.blueprint`: `build_instances_blueprint(source, nudger)` serves
  exactly `GET /_instances`, `POST /_instances` (201), `DELETE /_instances/<key>`
  (204, idempotent), `POST /_instances/<key>/rename`, and
  `POST /_instances/<key>/location`. Every mutating route calls the source, then
  `nudger.nudge()`, then answers; reads never nudge. A key that fails the key
  rule, a body that is not the route's shape, `UnknownActionError`,
  `InvalidParamsError`, `NotRenameableError`, and `LocationNotTrackedError` are
  `400`; `UnknownInstanceError` is `404`; `InstanceConflictError` is `409`;
  `NotReadyError` is `503`; any other library error is `500`. Every error body
  is `{"detail": "<message>"}`. Bodies are read with `force=True`, so a caller
  need not send a JSON content type. `build_instances_app(source, nudger)` is a
  Flask app that serves nothing but the blueprint, which is what the sidecar and
  the stub app run.
- `app_instances.interfaces`: `InstanceSourceInterface` (`list_instances`,
  `create_instance(action, params)`, `delete_instance(key)`,
  `rename_instance(key, title)`, `set_location(key, path)`; implementations
  must be thread-safe, the API is served threaded) and
  `InstanceNudgerInterface` (`nudge()`).
- `app_instances.data_types`: `InstanceStatus` (`working`, `idle`,
  `attention`, `stopped`, `error`), `InstanceLifetime` (`explicit`,
  `referenced`), `InstanceRecord` (the wire record; `model_dump(mode="json")`
  is what the API emits, with `last_active` anchored to UTC), and the request
  bodies `CreateRequest`, `RenameRequest`, `LocationRequest`.
- `app_instances.primitives`: `InstanceKey` (`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`),
  `InstanceKeyPrefix`, `InstanceUrl` (rooted with a single slash, at most 2048
  characters, no control characters, `{tab}` at most once), `LocationPath` (the
  same without the placeholder), `InstanceTitle` (non-blank, trimmed, at most
  256 characters), and `TitleTemplate` (must contain `{n}`).
- `app_instances.json_store`: `JsonStoreInstanceSource` keeps records in one
  `instances.json` (`{"version": 1, "instances": [record, ...]}`), rewritten
  atomically (temp file plus rename) under a process-wide lock, so the owning
  process must be the file's only writer; a stray temp file from a crashed write
  is ignored. Its one action is `new`, which mints the lowest free
  `<key_prefix>-<N>` (a deleted number is reused) and stores `params.path`
  (default `/`) as the URL; a location report replaces the URL and refreshes
  `last_active`. `key_prefix`, `title_template`, `lifetime`, `is_renameable`,
  and `is_location_tracked` are constructor fields. `app_store_path(name)` is
  the conventional `data/.apps/<name>/instances.json`; `allocate_key(prefix,
  taken_keys)` and `instance_number(prefix, key)` are the allocator, for sources
  that keep their own record of keys.
- `app_instances.nudge`: `ShellNudger(app_name, shell_url)` posts
  `POST <shell>/api/apps/<name>/changed` with a two-second timeout and logs an
  unreachable or refusing shell at debug level (until phase 7 of the model the
  shell answers `404`, which is expected); `shell_base_url()` resolves the shell
  exactly as `system/scripts/layout.py` does (`MINDS_WORKSPACE_SERVER_URL`,
  default `http://127.0.0.1:8000`).
- `app_instances.sidecar`: `run_sidecar(manifest_path, app_url, instances_url,
  child_argv, source)` starts the blueprint at `instances_url` on a daemon
  thread (werkzeug's threaded server), registers the app through
  `python3 system/scripts/forward_port.py --manifest <path> --url <app_url>`
  (after the listener is up, so the shell's first fetch succeeds), spawns the
  child, forwards `SIGTERM` and `SIGINT` to it, and returns the exit status to
  end the program with: the child's code, or 128 plus the signal number when a
  signal killed it. It must run on the main thread and from the repo root
  (the registration script and the registry are cwd-relative, like everything
  supervisord runs). The manifest must declare `instances = true` with an
  `instances_url` equal to the one served.
- `app_instances.testing`: `StubInstanceSource` (in-memory, records every
  call), `RecordingNudger`, `free_port`, `wait_until`, and
  `run_stub_app(port)` for the shell's tests in later phases; as a module it
  serves the stub app (`python -m app_instances.testing stub --port <port>`) or
  runs the sidecar over a JSON store (`... sidecar --manifest ... --app-url ...
  --instances-url ... --store ... -- <child argv>`).

Nothing mounts the blueprint yet: phase 3 (terminal), phase 4 (files), and
phase 5 (browser) of the model are its first users.

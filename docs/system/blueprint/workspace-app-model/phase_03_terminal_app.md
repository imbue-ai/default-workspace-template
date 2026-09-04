# Phase 3: the terminal app

Contracts: [contracts.md](contracts.md) sections 4.3 (terminal row), 5, and 10.
User-visible behaviour is unchanged: the same tabs, titles, session switching, renaming, and reattach after a container restart.

## Goal

Re-home the terminal's existing machinery into a small Python package that owns ttyd, serves the instances API, and reports session switches and renames to the shell through the generic tab route, without changing how tmux, the dispatch scripts, or the titles behave.

## Files

Created, under `system/apps/terminal/`:

- `pyproject.toml` (name `terminal-app`; depends on `app-instances`, `app-manifest`), `changelog/mngr-better-chat-app-arc.md`, `test_terminal_ratchets.py`.
- `src/terminal_app/main.py`: the entry point `terminal-app` (a click command whose options default to the fixed wiring, so tests can run it on scratch ports), which does what `run_ttyd.sh` did (writes the dispatch scripts into `data/.state/terminal/commands/`, installs the vendored OSC 52 client, writes the `server_registered` discovery event) and then calls `run_sidecar_app` with ttyd as the child, the app URL `http://localhost:7681`, and the instances URL `http://127.0.0.1:7682`; the app it builds mounts the instances blueprint and the hook route on one Flask app.
- `src/terminal_app/primitives.py`: `TmuxSessionName` (the instance-key alphabet without `.` and `:`, which tmux refuses), `TerminalTabId`, `ClientTty`, `Workdir`, `derive_terminal_title` (the frontend's derived-name rule), and `instance_url_for_session`.
- `src/terminal_app/data_types.py`, `interfaces.py`, `errors.py`: the hook event, the tmux session and client records, the store record and document, `TerminalPaths`; `TmuxInterface`, `TerminalSessionStoreInterface`, `ShellPosterInterface`; the app's errors (subclasses of `AppInstancesError`, so the blueprint answers them with a detail body).
- `src/terminal_app/tmux.py`: `SubprocessTmux`, the tmux client (`list-sessions`, `list-clients`, `kill-session -t =<name>`, `rename-session -t =<old> <new>`).
- `src/terminal_app/store.py`: `JsonTerminalSessionStore`, the app's own record of its terminals at `data/.apps/terminal/instances.json` (moved there from `data/.state/terminal/` at the start of phase 4, when contracts section 17 fixed where app data and machine state live) (`{"version": 1, "sessions": [{name, title, workdir}, ...]}`), written through the library's `read_json_document` and `write_json_document`. It is not the library's `JsonStoreInstanceSource`: that store can only mint `<prefix>-<N>` keys and cannot hold a renamed name such as `My-Build` or a URL carrying `{tab}`, and a renamed or restart-lost terminal must still list as `stopped` so its tab is not pruned.
- `src/terminal_app/sessions.py`: `TmuxSessionSource` (an `InstanceSourceInterface`): lists every non-`mngr-` tmux session whose name can be a key (`idle`, `last_active` from `#{session_activity}`) plus every remembered terminal not currently in tmux (`stopped`), allocates the lowest free `terminal-<N>` over both sets under a lock (the stored record is the reservation, so two creates before any attach get distinct names), deletes with `tmux kill-session` and forgets the record, renames as described under Behaviour, and returns `url = /?arg=_&arg=session&arg=<key>&arg={tab}[&arg=<workdir>]`.
- `src/terminal_app/hooks.py`: the route `POST /tmux-hook` the tmux hooks call, replacing `notify_terminal_session.py`'s target: for `session-changed` it maps the client pty to the tab id through the pty-to-tab files the dispatch writes and calls `POST <shell>/api/tabs/<tab_id>/instance {app: terminal, key: <session name>}`; for `session-renamed` it does the same for every tab whose pty is attached to the renamed session. Either event ends with a nudge, because the instance list may have changed: a switch may be the attach that created the session, and a rename re-keys one.
- `src/terminal_app/dispatch.py`: the dispatch script contents, moved verbatim from `run_ttyd.sh`, with the pty-to-tab directory under `data/.state/terminal/commands/clients/` baked in as an absolute path and the second argument of `session.sh` now the tab id (today the frontend-minted terminal id, from phase 7 the id the shell substituted for `{tab}`); plus the ttyd argv and the web client install.
- `src/terminal_app/discovery.py`: the `server_registered` event, in today's exact shape.
- `src/terminal_app/testing.py`: a fake `tmux` and a fake `ttyd` installed as executables on `PATH`.
- Unit tests for the primitives, the store, the tmux client and the source (over the fake `tmux`), the hook route (against `app_instances.testing.serve_recording_shell`, which records bodies now), the dispatch contents (inline snapshots), and the discovery event; `test_terminal_app.py` runs `terminal-app` as a process around the fakes.

Moved:

- `system/apps/terminal/notify_terminal_session.py` becomes the stdlib hook poster inside the package's `bin/` (still invoked by `terminal_tmux.conf` as a plain `python3` script; its only change is the target URL, `http://127.0.0.1:7682/tmux-hook`).
  A symlink at the old path keeps a tmux server that started before the move working, because a server keeps the hook commands it read at start; `# CLEANUP:` remove it in phase 11.
- `system/apps/terminal/terminal_tmux.conf` stays where it is; its hook lines point at the moved script.

Deleted: `system/apps/terminal/run_ttyd.sh`.

Modified:

- `system/supervisord.conf`: `[program:terminal]` runs `terminal-app`.
- `system/apps/terminal/README.md`; `.mngr/settings.toml` is untouched (the conf it sources did not move).
- `pyproject.toml` (root): the terminal leaves the workspace `exclude` list; it is a member like the other manifest apps until phase 9, and a tool.
- `system/libs/app_instances`: `run_sidecar_app(manifest_path, app_url, instances_url, child_argv, build_app)` is the seam an app with routes of its own uses (`run_sidecar` wraps it); `read_json_document` and `write_json_document` are the store's file handling made public; `canonical_name_from_title` and `is_name_conflict` are the shell's naming rule, shared.
- `system/apps/system_interface/.../server.py`: `POST /api/terminals/notify` uses a `terminal_id` in the body when the terminal app resolved one (the pty-to-tab files live in the terminal's state directory now, which the shell does not read); `# CLEANUP:` phase 7.
- `system/test_app_manifests.py`: a program whose command ends in an app's console script registers with the `MANIFEST_PATH` constant that script's module exports.
- `.agents/skills/migrate-workspace/scripts/migrate_workspace.py`: the port scan reports a registry row's `instances_url` port beside its `url` port, so 7682 counts toward collisions.
- `.agents/skills/update-self/scripts/update_self_test.py`: the terminal is a tool the apply refreshes.

## Behaviour

- Keys are session names, exactly the `terminal-<N>` names allocated today and any other non-`mngr-` session found in tmux whose name fits the key rule (a hand-made session with a space in its name is skipped at debug level).
- Rename follows the workspace's naming rule (the shell's `naming.py`, shared through `app_instances.primitives`): the title the user typed is kept as the instance's title and the session is renamed to its canonical true name (`My Build` becomes `My-Build`); a title that canonicalizes to nothing, or to more than 128 characters, is a bad title (400); one whose canonical form collides with another live or remembered terminal, case-insensitively, is a conflict (409); a title that canonicalizes to the current name only stores the title.
  `tmux rename-session` runs when the session is live, the hook fires, and every tab attached to that session is re-pointed at the new key through the shell's tab route, which is what today's `session-renamed` broadcast does to the tab's params.
  The old key disappears from the list and the new one appears; the shell's referenced-deletion never applies because terminal instances are `explicit`.
- A `new` with a `workdir` keeps it in the record and carries it as the URL's last argument, where the frontend puts it today, so the session is anchored there on first attach.
- A freshly created terminal lists as `stopped` until its first attach creates the session, exactly as the contract's terminal row says; the attach fires `client-session-changed`, whose handler makes the shell refetch.
- A session switch inside tmux re-points the tab the same way, as today.
- The instance title is the stored title when the user named the terminal, else the session name rendered as today's derived name (`Terminal 3` for `terminal-3`; any other name verbatim).
- Until phase 7 the shell has no tab route and no `changed` route; both posts fail quietly at debug level, and the shell's existing `/api/terminals/notify` keeps working because `terminal_tmux.conf` posts to the terminal app only; so this phase also keeps a compatibility forward: the hook route re-posts each event to the shell's existing `/api/terminals/notify` in today's shape plus the `terminal_id` it resolved, which the shell now prefers over its own lookup in the old `commands/ttyd/clients/` directory (nothing writes there any more). `# CLEANUP:` delete the forward and the shell's `terminal_id` handling in phase 7.

## Tests

- Source: listing merges tmux and the store; allocation fills gaps and honours reservations; delete kills and drops; rename runs `tmux rename-session`, canonicalizes the title, refuses collisions; the url carries the placeholder and the workdir.
- Hook route: `session-changed` resolves a pty to a tab id, posts the tab route and the compatibility forward, and nudges; `session-renamed` posts one tab route call per attached tab, one forward, and a nudge; a pty with no tab file posts nothing to the shell's routes but still nudges (the switch may be the attach that created the session).
- Dispatch scripts: byte-identical to today's except the directory and the tab argument (snapshot).
- The shell's existing terminal e2e tests keep passing unchanged in this phase.

## Manual verification

Two terminals, switch sessions inside one with `tmux switch-client`, rename one with `tmux rename-session`, reload the page, restart the workspace, and confirm titles and reattach behave exactly as before.

## Changelog entries

`system/apps/terminal/changelog/mngr-better-chat-app-arc.md` (new project), `system/changelog/mngr-better-chat-app-arc.md`, `system/libs/app_instances/changelog/mngr-better-chat-app-arc.md`, `system/apps/system_interface/changelog/mngr-better-chat-app-arc.md`, and `.agents/changelog/mngr-better-chat-app-arc.md` (the migrate-workspace port scan and the update-self tests).

## Exit criteria

The terminal's behaviour is indistinguishable from today from the user's seat, and `curl http://127.0.0.1:7682/_instances` lists the open sessions.

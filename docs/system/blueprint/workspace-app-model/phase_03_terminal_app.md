# Phase 3: the terminal app

Contracts: [contracts.md](contracts.md) sections 4.3 (terminal row), 5, and 10.
User-visible behaviour is unchanged: the same tabs, titles, session switching, renaming, and reattach after a container restart.

## Goal

Re-home the terminal's existing machinery into a small Python package that owns ttyd, serves the instances API, and reports session switches and renames to the shell through the generic tab route, without changing how tmux, the dispatch scripts, or the titles behave.

## Files

Created, under `system/apps/terminal/`:

- `pyproject.toml` (depends on `app-instances`, `app-manifest`), `changelog/mngr-better-chat-app-arc.md`, `test_terminal_ratchets.py`.
- `src/terminal_app/__init__.py`, `src/terminal_app/main.py`: the entry point `terminal-app`, which does what `run_ttyd.sh` does today (writes the dispatch scripts into `data/.state/terminal/commands/`, installs the vendored OSC 52 client, writes the `server_registered` discovery event) and then calls `run_sidecar` with ttyd as the child, the app URL `http://localhost:7681`, and the instances URL `http://127.0.0.1:7682`.
- `src/terminal_app/sessions.py`: `TmuxSessionSource` (an `InstanceSourceInterface`): lists every non-`mngr-` tmux session plus every name in the store (`data/.state/terminal/instances.json`, the library's JSON store) not currently in tmux, allocates `terminal-<N>` as today's allocator does (lowest free number over both sets, under a lock, with the in-flight reservation set), deletes with `tmux kill-session`, renames with `tmux rename-session` and re-keys nothing (see below), and returns `url = /?arg=session&arg=<key>&arg={tab}`.
- `src/terminal_app/hooks.py`: the route `POST /tmux-hook` the tmux hooks call, replacing `notify_terminal_session.py`'s target: for `session-changed` it maps the client pty to the tab id through the pty-to-tab files the dispatch writes and calls `POST <shell>/api/tabs/<tab_id>/instance {app: terminal, key: <session name>}`; for `session-renamed` it does the same for every tab whose pty is attached to the renamed session, and nudges.
- `src/terminal_app/dispatch.py`: the dispatch script contents, moved verbatim from `run_ttyd.sh`, with the pty-to-tab directory under `data/.state/terminal/commands/clients/` and `$3` now the tab id the shell substituted for `{tab}`.
- Unit tests for the source (over a fake `tmux` on `PATH`), the hook route, and the dispatch contents.

Moved:

- `system/apps/terminal/notify_terminal_session.py` becomes the stdlib hook poster inside the package's `bin/` (still invoked by `terminal_tmux.conf` as a plain `python3` script; its only change is the target URL, `http://127.0.0.1:7682/tmux-hook`).
- `system/apps/terminal/terminal_tmux.conf` stays where it is; its hook lines point at the moved script.

Deleted: `system/apps/terminal/run_ttyd.sh`.

Modified:

- `system/supervisord.conf`: `[program:terminal]` runs `terminal-app`.
- `system/apps/terminal/README.md`, `.mngr/settings.toml` (the `extra_provision_command` that writes `~/.tmux.conf` sources the same conf; the `commands/ttyd` path references become `data/.state/terminal/commands`).
- `pyproject.toml` (root): the terminal leaves the workspace `exclude` list, since it is now a tool rather than a member.

## Behaviour

- Keys are session names, exactly the `terminal-<N>` names allocated today and any other non-`mngr-` session found in tmux.
- Rename keeps today's semantics: `tmux rename-session` runs, the hook fires, and every tab attached to that session is re-pointed at the new key through the shell's tab route, which is what today's `session-renamed` broadcast does to the tab's params.
  The old key disappears from the list and the new one appears; the shell's referenced-deletion never applies because terminal instances are `explicit`.
- A session switch inside tmux re-points the tab the same way, as today.
- The instance title is the session name rendered as today's derived name (`Terminal 3` for `terminal-3`; any other name verbatim).
- Until phase 7 the shell has no tab route and no `changed` route; both posts fail quietly at debug level, and the shell's existing `/api/terminals/notify` keeps working because `terminal_tmux.conf` posts to the terminal app only; so this phase also keeps a compatibility forward: the hook route re-posts each event to the shell's existing `/api/terminals/notify` in today's shape. `# CLEANUP:` delete the forward in phase 7.

## Tests

- Source: listing merges tmux and the store; allocation fills gaps and honours reservations; delete kills and drops; rename runs `tmux rename-session`; the url carries the placeholder.
- Hook route: `session-changed` resolves a pty to a tab id and posts the tab route; `session-renamed` posts one tab route call per attached tab and a nudge; a pty with no tab file posts nothing.
- Dispatch scripts: byte-identical to today's except the directory and the tab argument (snapshot).
- The shell's existing terminal e2e tests keep passing unchanged in this phase.

## Manual verification

Two terminals, switch sessions inside one with `tmux switch-client`, rename one with `tmux rename-session`, reload the page, restart the workspace, and confirm titles and reattach behave exactly as before.

## Changelog entries

`system/apps/terminal/changelog/mngr-better-chat-app-arc.md` (new project), `system/changelog/mngr-better-chat-app-arc.md`, `.agents/changelog/mngr-better-chat-app-arc.md` if `.mngr/settings.toml` moves (it is `dev`).

## Exit criteria

The terminal's behaviour is indistinguishable from today from the user's seat, and `curl http://127.0.0.1:7682/_instances` lists the open sessions.

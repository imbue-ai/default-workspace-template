# terminal

The terminal tab: a web terminal served by [ttyd](https://github.com/tsl0922/ttyd),
supervised as the `terminal` program in `system/supervisord.conf`, which runs
the `terminal-app` entry point of this package (installed as its own uv tool by
`system/scripts/build_workspace.sh`, like every Python app with a manifest).

`terminal-app` does what the old launcher script did, then runs ttyd as the
sidecar's child (`app_instances.sidecar.run_sidecar_app`):

1. Writes the ttyd dispatch scripts into `data/.state/terminal/commands/`
   (`dispatch.py`): `session.sh` attaches to a `terminal-N` tmux session by the
   id recorded under `data/.state/terminal/sessions/<key>` (falling back to
   `tmux new-session -A` by name, which creates the session when tmux lost it)
   and records the tab's pty under `commands/clients/<tab id>`; `workdir.sh`
   opens a shell in a directory;
   `agent.sh` attaches to an mngr agent's tmux window for the chat UI's
   terminal back face. The ttyd URL `?arg=_&arg=<key>&arg=...` runs
   `commands/<key>.sh` with the remaining arguments.
2. Decompresses the OSC 52-capable ttyd web client vendored with the
   `mngr_ttyd` plugin (`system/vendor/mngr/libs/mngr_ttyd/`) and serves it via
   `ttyd -I`, falling back to the stock client (with a warning) when the asset
   is missing or will not decompress.
3. Appends the `server_registered` discovery event to
   `$MNGR_AGENT_STATE_DIR/events/servers/events.jsonl` (`discovery.py`).
4. Serves the instances API (`GET/POST /_instances`, ...) on `127.0.0.1:7682`,
   the manifest's `instances_url`, registers `app.toml` and the ttyd port 7681
   through `system/scripts/forward_port.py`, and runs
   `ttyd -p 7681 -a -t disableLeaveAlert=true [-I index.html] -W bash -c <dispatch>`
   as its child, forwarding `SIGTERM` and `SIGINT` to it and exiting with its
   status.

## Instances

`sessions.py` (`TmuxSessionSource`) serves the terminal row of
`docs/system/blueprint/workspace-app-model/contracts.md` section 4.3. Keys are
the names the app allocates, `terminal-N`, and never change. The list is every
non-`mngr-` tmux session whose name can be an instance key (`idle`; a hand-made
session with, say, a space in its name is skipped) plus every terminal the store
remembers that tmux no longer has (`stopped`). A remembered terminal is matched
to its live session by tmux's immutable session id, so a session renamed inside
tmux keeps its key and its title; a session no record holds by id falls back to
the record of its name (one from before the app kept ids, or a session the
dispatch created on attach), and one with no record at all lists under its own
name. The URL is `/?arg=_&arg=session&arg=<key>&arg={tab}[&arg=<workdir>]`; the
shell substitutes the tab id, and `session.sh` receives it as its second
argument. A terminal created through `new` always carries a workdir: the
`workdir` param when the create gave one, else the directory the app runs from
(the workspace root under supervisord); only records written before that default
lack one.

- `new` (optional `workdir`) allocates the lowest free `terminal-<N>` over the
  live and remembered names and creates the tmux session at once (`tmux
  new-session -d -s <key> -c <workdir> <session command>`), so the terminal is
  `idle` from the start. The session command runs the login shell through
  `system/services/oom_priority/bin/oom_tag_service.py terminal-session`, which
  puts the shell and everything run in it in the `terminal-session` memory band
  (the user-service level; a pane would otherwise inherit the tmux server's
  protected 0). The session id is recorded in the store and under
  `data/.state/terminal/sessions/<key>`, which `session.sh` attaches by.
- At startup the app recreates the session of every remembered terminal tmux
  no longer has (a container restart clears the server), adopts a live one it
  finds by id or by name, and leaves alone a terminal the user stopped
  (`is_stopped` in the store).
- Delete kills the session (by id when known, else `tmux kill-session -t
  =<name>`), forgets the record, and drops the id file; an `mngr-` session is
  refused.
- Rename changes only the title: the key and the tmux session name stay. A
  title that canonicalizes to nothing (`app_instances.primitives.canonical_name_from_title`)
  is a bad title (400); one whose canonical form collides with another
  terminal's title, case-insensitively, is a conflict (409). Allocator-minted
  names keep deriving their title (`Terminal 3` for `terminal-3`) until renamed.
- Location is not tracked (400).

The store, `data/.apps/terminal/instances.json` (`store.py`; app data, beside
every other app's instance records, while `data/.state/terminal/` holds only
the dispatch scripts, pty records, and session id files), holds
`{name, title, workdir, session_id, is_stopped}` per remembered terminal and is
written atomically through the `app_instances` JSON document helpers.

## tmux hooks

`terminal_tmux.conf` holds the in-memory-persistent-terminals tmux settings
(scrollback, window sizing, and the tab-title tracking hooks); it is sourced
from `~/.tmux.conf`, which the main create template writes. Its hooks call
`bin/notify_terminal_session.py`, the standard-library helper that posts
`{kind, client_tty, session_name, session_id}` to `POST /tmux-hook` on 7682
(`hooks.py`) when a client switches sessions or a session is renamed. For a
switch, the route maps the client's pty to its tab through `commands/clients/`,
resolves the terminal whose session the client now shows (by the session id;
a record without one adopts the session of its name, which is how the app
learns the id of a session `session.sh` created on attach), and re-points the
tab through the shell's `POST /api/tabs/<tab_id>/instance` (contracts section
5). A rename changes no key and no title (the shell tab's title is the record's)
and so only nudges. Either event nudges the shell, since the instance list may
have changed.

`notify_terminal_session.py` at this folder's root is a symlink into `bin/` for
tmux servers that started before the helper moved (a server keeps the hook
commands it read at start).

## Tests

`uv run pytest system/apps/terminal` from the repo root. The unit tests drive
the real source and tmux client over a fake `tmux` on `PATH`
(`testing.py`); `test_terminal_app.py` runs `terminal-app` as a process around
a fake `ttyd`.

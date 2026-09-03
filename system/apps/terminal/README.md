# terminal

The terminal tab: a web terminal served by [ttyd](https://github.com/tsl0922/ttyd),
supervised as the `terminal` program in `system/supervisord.conf`, which runs
the `terminal-app` entry point of this package (installed as its own uv tool by
`system/scripts/build_workspace.sh`, like every Python app with a manifest).

`terminal-app` does what the old launcher script did, then runs ttyd as the
sidecar's child (`app_instances.sidecar.run_sidecar_app`):

1. Writes the ttyd dispatch scripts into `data/.state/terminal/commands/`
   (`dispatch.py`): `session.sh` attaches to (or creates) a named persistent
   `terminal-N` tmux session and records the tab's pty under
   `commands/clients/<tab id>`; `workdir.sh` opens a shell in a directory;
   `agent.sh` attaches to an mngr agent's tmux window for the chat UI's
   terminal back face. The ttyd URL `?arg=_&arg=<key>&arg=...` runs
   `commands/<key>.sh` with the remaining arguments.
2. Decompresses the OSC 52-capable ttyd web client vendored with the
   `mngr_ttyd` plugin (`system/vendor/mngr/libs/mngr_ttyd/`) and serves it via
   `ttyd -I`, falling back to the stock client when the asset is missing.
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
tmux session names. The list is every non-`mngr-` tmux session (`idle`) plus
every terminal the store remembers that tmux no longer has (`stopped`, which is
how a tab reattaches after a container restart cleared the tmux server). The
URL is `/?arg=_&arg=session&arg=<key>&arg={tab}[&arg=<workdir>]`; the shell
substitutes the tab id, and `session.sh` receives it as its second argument.

- `new` (optional `workdir`) allocates the lowest free `terminal-<N>` over the
  live and remembered names and records it; the session itself is created on
  first attach by `tmux new-session -A`, as before.
- Delete kills the session (`tmux kill-session -t =<name>`) and forgets it;
  an `mngr-` session is refused.
- Rename follows the workspace's naming rule (the shell's `naming.py`, shared
  through `app_instances.primitives.canonical_name_from_title`): the title the
  user types is kept as the title, and the session is renamed to its canonical
  form (`My Build` becomes `My-Build`), so a rename is a re-key. A title that
  canonicalizes to nothing is a bad title (400); one that collides with another
  terminal, case-insensitively, is a conflict (409). Allocator-minted names keep
  deriving their title (`Terminal 3` for `terminal-3`) until renamed.
- Location is not tracked (400).

The store, `data/.state/terminal/instances.json` (`store.py`), holds
`{name, title, workdir}` per remembered terminal and is written atomically
through the `app_instances` JSON document helpers.

## tmux hooks

`terminal_tmux.conf` holds the in-memory-persistent-terminals tmux settings
(scrollback, window sizing, and the tab-title tracking hooks); it is sourced
from `~/.tmux.conf`, which the main create template writes. Its hooks call
`bin/notify_terminal_session.py`, the standard-library helper that posts
`{kind, client_tty, session_name, session_id}` to `POST /tmux-hook` on 7682
(`hooks.py`) when a client switches sessions or a session is renamed. The route
maps the client's pty to its tab through `commands/clients/`, re-points the tab
through the shell's `POST /api/tabs/<tab_id>/instance` (contracts section 5;
the shell answers 404 until phase 7 of the model), and nudges the shell, since
either event may have changed the instance list (a switch may be the attach
that created the session; a rename re-keys one). Until phase 7 it also
forwards each event to the shell's
`/api/terminals/notify`, with the resolved `terminal_id`, so today's
`terminal_session` broadcast keeps tab titles live.

`notify_terminal_session.py` at this folder's root is a symlink into `bin/` for
tmux servers that started before the helper moved (a server keeps the hook
commands it read at start).

## Tests

`uv run pytest system/apps/terminal` from the repo root. The unit tests drive
the real source and tmux client over a fake `tmux` on `PATH`
(`testing.py`); `test_terminal_app.py` runs `terminal-app` as a process around
a fake `ttyd`.

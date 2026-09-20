# terminal

The terminal app: a web terminal served by [ttyd](https://github.com/tsl0922/ttyd),
run as two supervisord programs declared in `system/supervisord.conf.d/` and both
installed as entry points of this package's uv tool (`system/scripts/build_workspace.sh`,
like every Python app with a manifest):

- `terminal` (`terminal-app`) is the terminal origin. It serves the **wrapper pages**
  on 7681 (`pages.py`): `/?session=<name>` frames the session's ttyd page from the
  pty origin, reports its path and the session's title to the shell through the app
  contract (imported from the shell origin), re-points the frame on `shell:navigate`,
  and passes the shell's `ttyd-focus` grant on to ttyd; `/new[?workdir=]` allocates
  the lowest free `terminal-N`, creates its tmux session, and redirects to its page;
  `/api/sessions/<name>` is what the page refreshes from, and `/api/health` the probe.
  It appends the `server_registered` discovery event to
  `$MNGR_AGENT_STATE_DIR/events/servers/events.jsonl` (`discovery.py`), recreates the
  remembered sessions, registers `app.toml` and 7681 through
  `system/scripts/forward_port.py`, and waits for `SIGTERM`.
- `terminal-pty` (`pty_main.py`) is ttyd itself, on its own internal origin
  (`system/apps/terminal_pty/app.toml`, port 7683), framed by the wrapper. It writes
  the ttyd dispatch scripts into `data/.state/terminal/commands/` (`dispatch.py`):
  `session.sh` attaches to a `terminal-N` tmux session by the id and creation time
  recorded under `data/.state/terminal/sessions/<name>` (falling back to `tmux
  new-session -A` by name, which creates the session when tmux lost it, when there is
  no record, or when the session under that id is a later server's); `workdir.sh` opens a shell in a
  directory; `agent.sh` attaches to an mngr agent's tmux window for the chat UI's
  terminal back face. The ttyd URL `?arg=_&arg=<key>&arg=...` runs
  `commands/<key>.sh` with the remaining arguments. It decompresses the OSC 52-capable
  ttyd web client vendored with the `mngr_ttyd` plugin
  (`system/vendor/mngr/libs/mngr_ttyd/`) and serves it via `ttyd -I`, falling back to
  the stock client (with a warning) when the asset is missing or will not decompress,
  registers its manifest and port, and execs
  `ttyd -p 7683 -a -t disableLeaveAlert=true [-I index.html] -W bash -c <dispatch>`.

## Terminals

`sessions.py` (`TmuxSessionSource`) keeps the app's own terminals. Names are the
ones the app allocates, `terminal-N`, and never change. The list is every
non-`mngr-` tmux session whose name can be a session name (a hand-made session
with, say, a space in its name is skipped) plus every terminal the store
remembers that tmux no longer has (stopped). A remembered terminal is matched
to its live session by tmux's session id together with the session's creation
time (an id is unique only for one server's lifetime: the server a container
restart brings up hands the same ids out again, so the creation time tells a
terminal's session apart from a later server's under the same id; a side that
knows no creation time matches on the id alone), so a session renamed inside
tmux keeps its name and its title; a session no record holds falls back to
the record of its name when that record's own session is not live (a record
that holds no id, or a session the dispatch created on attach; a session
that only carries the old name of a terminal whose own session is live is
skipped, whichever tmux lists first), and one with no record at all lists under
its own name. A window of the terminal shows the wrapper page, `/?session=<name>`;
the wrapper frames `/?arg=_&arg=session&arg=<name>[&arg=<workdir>]` on the pty
origin, and `session.sh` receives the name and the directory as its arguments.
A terminal created through `new` always carries a workdir: the `workdir` param
when the create gave one, else the directory the app runs from (the workspace
root under supervisord); a record of a hand-made session holds none, and a
session recreated for it starts in the default.

- `new` (optional `workdir`) allocates the lowest free `terminal-<N>` over the
  live and remembered names and creates the tmux session at once (`tmux
  new-session -d -s <name> -c <workdir> <session command>`), so the terminal
  is live from the start. The session command runs the login shell through
  `system/services/oom_priority/bin/oom_tag_service.py terminal-session`, which
  puts the shell and everything run in it in the `terminal-session` memory band
  (the user-service level; a pane would otherwise inherit the tmux server's
  protected 0). The session id and creation time are recorded in the store and, as two
  lines, under `data/.state/terminal/sessions/<name>`, which `session.sh`
  attaches by.
- At startup the app recreates the session of every remembered terminal tmux
  no longer has (a container restart clears the server), adopts a live one it
  finds by id or by name, and leaves alone a terminal the user stopped
  (`is_stopped` in the store).
- The source also carries the terminal's own verbs, which no route offers yet:
  delete kills the session (by id when known, else `tmux kill-session -t
  =<name>`), forgets the record, and drops the id file, refusing an `mngr-`
  session; rename changes only the title (the name and the tmux session name
  stay; a title that canonicalizes to nothing under
  `app_manifest.primitives.canonical_name_from_title` is refused, and one whose
  canonical form collides with another terminal's title, case-insensitively, is
  a conflict); stop kills the session and remembers the terminal as stopped (a
  hand-made session gains a record so it can be started); start recreates the
  session in the record's workdir, and is a no-op for a live terminal.
  Allocator-minted names keep deriving their title (`Terminal 3` for
  `terminal-3`) until renamed.

Session switching inside tmux is not reported to the shell: a window shows the
session in its URL, and a reload reattaches to that session.

The store, `data/.apps/terminal/instances.json` (`store.py`; app data, beside
the other apps' stored records, while `data/.state/terminal/` holds only the
dispatch scripts and session id files), holds
`{name, title, workdir, session_id, session_created, is_stopped}` per
remembered terminal and is rewritten atomically. It keeps the file name it has
always had, so a workspace upgraded onto this release keeps its terminals.

`terminal_tmux.conf` holds the in-memory-persistent-terminals tmux settings
(scrollback and window sizing); it is sourced from `~/.tmux.conf`, which the
main create template writes.

## Tests

`uv run pytest system/apps/terminal` from the repo root. The unit tests drive
the real source and tmux client over a fake `tmux` on `PATH`
(`testing.py`); `test_terminal_app.py` runs `terminal-app` and `terminal-pty`
as processes, the latter around a fake `ttyd`.

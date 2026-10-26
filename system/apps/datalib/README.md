# datalib

The Datalib app: datalib's own web UI -- the Manage screen and the Add/Edit
source wizard served by `datalib-http` -- over the store the `datalib` skill
writes, so a source added through the wizard is what the agent searches and a
sync the agent runs is what the UI shows. Supervised as the `datalib` program,
defined in `datalib.conf` here, whose program line runs `launch_datalib_http.py`
under a plain `python3`.

datalib is not announced to users yet, so a workspace starts with no datalib
program at all: supervisord reads only `system/supervisord.conf.d/`, and
`datalib.conf` is not there. The `datalib` skill, used only by a user who asked
for datalib by name, runs `.agents/skills/datalib/scripts/install_datalib.sh`,
which installs the binaries, copies `datalib.conf` into
`system/supervisord.conf.d/` and loads it with `supervisorctl reread` and
`supervisorctl update`. The program autostarts, so from then on that workspace
runs it like any other app: at boot, and after a backup restore. The installed
copy is gitignored, so it is not a change in the workspace's repository.

An app is listed only once its program has registered it, so the Datalib app
appears in a workspace when the install script first starts it, and stays
listed from then on.

The app is a manifest, an icon, and that launcher; it is not a Python package,
and it is excluded from the uv workspace in the root
`pyproject.toml`. The launcher uses only the standard library, so the app does
not depend on the root venv being intact.

The server is `datalib-http` from the datalib release pinned in
`.agents/skills/datalib/scripts/install_datalib.sh` (the same pin as the
skill; `test_datalib_pin.py` checks the two agree). That script installs the
release into `~/.local/share/datalib/<version>/` and links it into
`~/.local/bin`. Nothing runs it at boot: the skill runs it the first time it
is used, so a workspace that never uses datalib downloads nothing. The launcher does five things, in order:

1. Moves a store written at `data/.skills/datalib` itself into the `Default`
   library, if there is one (see "The libraries folder" below).
2. Reads the API token (see below).
3. Registers the app and the datalib-http port 8731 through
   `system/scripts/forward_port.py`.
4. Sets its own `oom_score_adj` to the agent-subprocess band (900), which
   datalib-http and every sync step it starts inherit. Under memory pressure
   datalib is then shed before any agent or service. The program line starts
   it in the `user` band (200), the band the manifest's `priority` names; the
   OOM backstop only ever raises a process to its band, so it leaves 900 alone.
5. Replaces itself with `~/.local/bin/datalib-http --no-open
   data/.skills/datalib/Default`, with `DATALIB_BIND=127.0.0.1:8731` and the
   token in `DATALIB_TOKEN`. From then on supervisord supervises datalib-http
   directly.

If it is started before that script has installed the binary, the launcher
exits without registering anything and supervisord retries it with its
backoff, so the app never appears with nothing behind it.

## The libraries folder

The skill's folder, `data/.skills/datalib`, is laid out like the desktop
app's `~/Datalib`: a libraries folder with one folder per library. The app
serves one library, `Default`, so the data root on its command line is
`data/.skills/datalib/Default`, and the skill's scripts and `SKILL.md` name
the same path. A second library would need a program, port, token and window
of its own; the layout only leaves room for one.

A workspace that used datalib before the libraries folder existed wrote its
store at `data/.skills/datalib` itself. The launcher moves such a store into
`Default` before it reads the token: it renames the folder to a staging
sibling (`data/.skills/datalib.moving-to-Default`), makes a fresh folder, and
renames the staging sibling into it. Each rename is atomic, and a start that
finds the staging sibling finishes the second rename, so an interruption
between the two loses nothing. The move follows the desktop app's rules for
moving a library:

- only a store at the real libraries folder is moved; a `--data-root` that
  is not directly inside it (a test's) is a store somewhere else, and its
  parent is never touched;
- a store a datalib process still holds (a `flock` on `system/lock` or
  `system/runner-lock`, say a `datalib-dag` an agent ran that straddles the
  restart) is left where it is, and the launcher exits so supervisord retries
  it; the move happens on the start that finds the store released;
- a top-level `data_root` in the moved `config.toml` that names the old folder
  is dropped, since `datalib-dag` writes where that key says; any other value
  is kept;
- a folder that holds a store of its own *and* a non-empty `Default` is left
  alone and the launcher refuses to start, naming both and what it found at
  each, since it cannot tell which one is the user's.

## The token

`datalib-http` requires its per-process API token on every route: loopback
does not keep a web page out, and `PUT /api/config` runs shell commands. A
browser gets in by loading `/?token=<token>` once, which sets a session cookie
and redirects to `/`.

A window opens at a launch path's `path` with the launch path's `presets` as
the query string. `app.toml` declares one launch path, `ui`, at `/`. The
launcher writes a copy of `app.toml` to `data/.state/datalib-app/` with
`presets = { token = "<token>" }` appended to that launch path, and registers
the copy, so a window opens at `/?token=<token>`. `app.toml` must keep its
`[[launch_paths]]` table last for the append to land in it; the launcher
checks this and refuses to start otherwise.

The token itself is `data/.skills/datalib/Default/system/api-token`: the launcher reads
it at start and hands it to `datalib-http` through `DATALIB_TOKEN`, so it stays
the same across restarts (a browser already holding the cookie stays signed
in) and the skill can read the same file for its bearer token. When the file
is missing or unusable the launcher mints one, and `datalib-http` writes it
there.

## Being framed

`datalib-http`'s app page refuses to be framed unless
`DATALIB_FRAME_ANCESTORS` names who may frame it, and a proxy's own
`frame-ancestors` cannot loosen that: the browser enforces both. The launcher
sets one fixed list (`FRAME_ANCESTORS` in `launch_datalib_http.py`):

- the desktop app: `http://localhost:*`, `http://127.0.0.1:*` and
  `https://*.localhost:*`, which only pages served from the laptop match;
- a share: `https://*.<content domain>` for each of Imbue's content domains,
  and each hosted chrome origin. These also admit other users' shares; the
  share gateway appends a `frame-ancestors` for this workspace's own share
  domain, and the browser enforces that too.

A content domain or chrome origin Studio adds needs adding here.

The session cookie works inside the frame because `datalib-http` sets it twice:
`SameSite=Lax`, and `SameSite=None; Secure; Partitioned` for a frame on another
site (imbue-ai/datalib#1318). An older `datalib-http` ignores
`DATALIB_FRAME_ANCESTORS` and stays unframable.

## Staying up with no window

`app.toml` sets `stop_when_no_windows = false`: a sync started from the Manage
screen runs inside `datalib-http` and has to finish after the window closes.

## Tests

`uv run pytest system/apps/datalib` from the repo root.
`launch_datalib_http_test.py` pins the token handling, the move of a store
into the default library, and the registered manifest copy;
`test_datalib_app.py` runs the launcher as a process, through
the real `forward_port.py` and around a fake `datalib-http`
(`datalib_launcher_testing.py`); `test_datalib_pin.py` holds the skill to the
version the install script installs and to the data root the app serves, and
keeps the program out of `system/supervisord.conf.d/`.

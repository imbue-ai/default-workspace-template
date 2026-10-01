# workspace_layout

The shell's layout routes as one typed client: what an app or an agent uses to
read the desktop and put a window on a client's screen. The routes themselves
are the desktop interface's contract
(`docs/system/blueprint/desktop-interface/contracts.md`, section 5 for the
desktops, clients, and client-activity routes, section 8 for the op route);
this library is how every Python caller reaches them, so the request and
answer shapes live in one place the shell and its callers share.

## API

- `workspace_layout.shell_url`: `shell_base_url()` (`MINDS_WORKSPACE_SERVER_URL`,
  else `http://127.0.0.1:8000`) and the route constants.
- `workspace_layout.primitives`: the identifiers the wire carries
  (`ClientId`, `DesktopId`, `WindowId`, `WallpaperName`, and the
  `IfPresent`, `WallpaperKind`, `ClientActivityKind` values). A value off its
  rule raises `InvalidLayoutValueError`.
- `workspace_layout.ops`: the op route's request side. The op names and
  `is_known_op`; `OpRequester` and `parse_op_requester` (who asked:
  `{app, marker}` or nothing); `DesktopOpArguments`, the arguments model the
  shell reads every op with; `ClientActivityReport`, the client-activity
  route's body; the typed requests (`ShowRequest`, `OpenRequest`,
  `WindowRequest`, `NavigateRequest`, `PlaceRequest`), each naming its target
  client (None leaves the choice to the shell: the requester's client, else the
  one connected client); and the pure functions that spell a request as the
  wire does (`op_request_body`, `show_op_arguments`, ...).
- `workspace_layout.answers`: the answers, parsed with unknown fields ignored
  (the shell's to add to): `DesktopOpAnswer`, `OpenAnswer`, `ShowAnswer`,
  `ConnectedClient`, `DesktopSummary`. A list answer skips one entry it
  cannot read, with a warning, and raises when none of a non-empty list reads.
- `workspace_layout.errors`: `WorkspaceLayoutError`; `InvalidLayoutValueError`;
  and `ShellOpError`, raised by every op that did not happen, as
  `ShellUnreachableError` (down, restarting, timed out), `ShellRefusedOpError`
  (an error status, carrying it; `WindowPoppedOutError` for the shell's refusal
  to move a window the client popped out into its own window, which a
  `PlaceRequest` with `is_forced` overrides), or `ShellAnswerMalformedError` (a
  success status with a body that is not the answer).
- `workspace_layout.interfaces.ShellLayoutInterface`: `show`, `open`, `focus`,
  `navigate`, `place`, `close`, `refresh`, `connected_clients`, `desktops`, and
  `record_client_activity` (best-effort: an unreachable or failing shell is a
  debug log, one that refuses the report a warning, and it never raises).
- `workspace_layout.client`: `ShellLayoutClient` (the shell over loopback, with
  the requester every op carries and a per-request timeout), `DisconnectedShell`
  (nobody connected and nothing to show on: the stand-in where no shell is
  wired), `requester_from_environment()` (an agent's own chat, from
  `MINDS_CHAT_ID`, else `MNGR_AGENT_ID`), and `request_shell`, the one request
  as it came back.
- `workspace_layout.windows`: what an app with window-bound resources sweeps
  against (`docs/system/specs/window-bound-resources.md` section 4.2):
  `read_app_window_paths(shell_url, app)` (every window path of the app, or
  None when the shell could not be read, never "no windows"),
  `window_paths_of_app`, and `window_query_value`.
- `workspace_layout.cli`: the `workspace-layout` console script (below).
- `workspace_layout.testing`: `FakeShell`, an in-memory `ShellLayoutInterface`
  that records every request and answers or refuses as a test sets it;
  `LoopbackShell`, a stand-in for the shell's routes over loopback that reads
  every op body as the shell does (`describe_op_body_problem`) and refuses one
  it would refuse; and wire-shaped `desktop_answer` and `window_json` builders.

## The `workspace-layout` command

`uv run workspace-layout <subcommand>`, from the repo root, is how an agent
reads and arranges the desktop; the `manage-desktop` skill is its guide and
`uv run workspace-layout --help` its reference. Every subcommand posts one op
to the shell's op route under the calling agent's own chat as the requester
(`MINDS_CHAT_ID`, else `MNGR_AGENT_ID`), except `desktops` and `list`, which
read `GET /api/inventory`. Descriptions go to stderr; stdout carries only a
window id (`open`, `show`), the JSON of the read commands, and a desktop's
shortcuts after a `shortcut` write. Exit codes are `0` (done), `1` (refused or
unreachable), `3` (the shell or an app cannot act right now: retry), and `4`
(the window is popped out into its own window: pass `--force`, which every
mutating subcommand takes, or leave it to the user). A summary notes when an op
raised a popped-out window in its own window, brought one back, left an `open
--beside` unpaired, or landed for a client with no desktop window open.

`show <app> --path P [--showing P ...] [--repoint PAGE ...]` runs the shell's
`show` op: it raises a window already at the path (or at a `--showing` path),
else points an on-screen window on one of the `--repoint` pages at it, else the
app's pinned window, else opens one.

The command replaced `system/scripts/layout.py`, keeping its subcommands,
flags, output, and exit codes; the hints for retired verbs and spellings name
`uv run workspace-layout`. Unlike the script it runs in the root venv, so it
shares the request models the shell reads and the app name rule of
`app_manifest`.

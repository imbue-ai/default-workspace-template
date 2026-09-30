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
  (an error status, carrying it), or `ShellAnswerMalformedError` (a success
  status with a body that is not the answer).
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
- `workspace_layout.testing`: `FakeShell`, an in-memory `ShellLayoutInterface`
  that records every request and answers or refuses as a test sets it;
  `LoopbackShell`, a stand-in for the shell's routes over loopback that reads
  every op body as the shell does (`describe_op_body_problem`) and refuses one
  it would refuse; and wire-shaped `desktop_answer` and `window_json` builders.

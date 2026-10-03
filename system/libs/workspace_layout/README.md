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
- `workspace_layout.primitives`: the values the wire carries. The ids
  (`ClientId`, `DesktopId`, `WindowId`, `UserId`, `WallpaperName`), a window's
  `WindowPath`, `WindowPage`, and `WindowTitle`, and the enums: `LayoutOp` (every
  op the op route knows), `WindowState`, `ShowOutcome`, `SpecialWindow` (`self`,
  `pinned`), `IfPresent`, `WallpaperKind`, `ShortcutTargetKind`,
  `ClientActivityKind`. A value off its rule raises `InvalidLayoutValueError`.
- `workspace_layout.records`: the desktop's records (contracts section 4), as
  the shell stores them and as its answers carry them: `Frame` and `GridCell`
  (with `parse_frame` and `parse_cell` for their `x,y,width,height` and
  `column,row` text forms), `Wallpaper`, `ShortcutTarget`, `DesktopShortcut`,
  `Window`, `Desktop`, `WindowPlacement`, `DesktopLayout`, `StoredWindowPath`,
  `ClientRecord`, `EntryPresentation`, and the views an answer carries
  (`WindowView`, `DesktopView`, `DesktopLayoutView`).
- `workspace_layout.ops`: the op route's request side. One body model per op
  (or per group of ops that take the same arguments: `WindowOpBody` for
  `focus`, `minimize`, `restore`, `maximize`, and `close`, `InventoryBody` for
  `desktops` and `list`), each carrying its own arguments model (`ShowArgs`,
  `OpenArgs`, `WindowArgs`, `PlaceArgs`, `NavigateArgs`, `RefreshWindowArgs` or
  `RefreshAppArgs` (a `refresh` is of an app when it names one), `LoadArgs`, the shortcut and wallpaper arguments), and
  `OpBody`, their union, keyed on `op`. The client-scoped arguments extend
  `OpTarget` (`client`, `desktop`; None leaves the choice to the shell: the
  requester's client, else the one connected client, and the client's active
  desktop). A body refuses an argument its op does not take and any rule its
  arguments break (`place` takes a `state` of `PLACEABLE_STATES` or a `frame`;
  an `open` names a path or a launch path; a `window` or `beside` spells a
  window, and a `desktop` is not empty); `context`, `desktops`, and `list`
  read none and ignore what they are sent. `op_request_body` spells a body as
  the wire does (only the arguments the caller set) and `parse_op_body` reads
  one as the shell does, naming every argument it refuses. Also
  `parse_window_reference` (a window id, `self`, `pinned`, or an app name),
  `OpRequester`, `parse_op_requester`, `requester_spelling`, and
  `ClientActivityReport`.
- `workspace_layout.answers`: every answer the shell gives, which the shell
  builds from these models and a caller parses with them: `DesktopOpAnswer`,
  `OpenAnswer`, `ShowAnswer`, `TransientOpAnswer`, `ContextAnswer`,
  `InventoryDocument` (and `InventoryOpAnswer`), `DesktopsListing`,
  `ClientsListing`, and the `layout_op` WebSocket message (`LayoutOpMessage`).
  `parse_answer` and `parse_listing` read an answer with unknown fields ignored
  (the shell's to add to); a listing skips one entry it cannot read, with a
  warning, and raises when none of a non-empty list reads. A caller reads an
  answer through them, never with a model's own `model_validate`, which is as
  strict as the shell's files are. The tolerance covers added fields only: a
  value outside an enum or a rule (a new `ShowOutcome`, say) makes the answer
  malformed.
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
  against (`docs/system/specs/window-bound-resources.md` sections 4.2 and 4.6):
  `read_app_window_paths(shell_url, app)` (every window path of the app, or
  None when the shell could not be read, never "no windows"),
  `window_query_value`, and the `WindowClosedHint` the
  shell posts when a window closes, with `parse_window_closed_hint`.
- `workspace_layout.cli`: the `workspace-layout` console script (below).
- `workspace_layout.testing`: `FakeShell`, an in-memory `ShellLayoutInterface`
  that records every request and answers or refuses as a test sets it;
  `LoopbackShell`, a stand-in for the shell's routes over loopback that reads
  every op body as the shell does (`describe_op_body_problem`) and refuses one
  it would refuse, serving inside a `with` block; and builders of the records and answers (`fake_window`,
  `fake_desktop`, `fake_app`, `connected_client`, `desktop_answer`).

## Ops by kind

A property that holds for some ops and not others is carried by the op's body
model (its arguments, and so what the shell does with them) or by a `Literal`
subset of `LayoutOp` that a handler takes, as `WindowOp` is; never a set of op
names. `parse_op_body` picks an op's body model with an exhaustive `match` over
`LayoutOp`, and the shell dispatches on the body with another, both ending in
`assert_never`: an op added to `LayoutOp` fails type checking until it has a
body, and a body added to `OpBody` until the shell handles it. The arguments
models share their fields through private bases rather than subclassing one
another, so one op's arguments never type-check as another's.

## The `workspace-layout` command

`uv run workspace-layout <subcommand>`, from the repo root, is how an agent
reads and arranges the desktop; the `manage-desktop` skill is its guide and
`uv run workspace-layout --help` its reference. Every subcommand posts one op
to the shell's op route under the calling agent's own chat as the requester
(`MINDS_CHAT_ID`, else `MNGR_AGENT_ID`), except `desktops` and `list`, which
read `GET /api/inventory`. Descriptions go to stderr; stdout carries only a
window id (`open`, `show`), the JSON of the read commands, and a desktop's
shortcuts after a `shortcut` write. Exit codes are `0` (done), `1` (refused or
unreachable), and `3` (the shell or an app cannot act right now: retry).

`show <app> --path P [--showing P ...] [--repoint PAGE ...]` runs the shell's
`show` op: it raises a window already at the path (or at a `--showing` path),
else points an on-screen window on one of the `--repoint` pages at it, else the
app's pinned window, else opens one.

The command runs in the root venv, so it builds every op from the body models
the shell reads and reads every answer through the answer models the shell
builds. `place` takes `--state snapped-left|snapped-right|maximized` (the
`WindowState` the op sets, which its description names: `as snapped-left`) or
`--frame x,y,width,height`; the retired `--zone` is refused with that form.

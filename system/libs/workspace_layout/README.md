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
  else `http://127.0.0.1:8000`), the route constants, and
  `POPPED_OUT_REFUSAL_STATUS` (`423`, the op route's refusal to move a window
  the target client popped out into its own window).
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
  (an error status, carrying it; `WindowPoppedOutError` for the shell's refusal
  to move a window the client popped out into its own window, which `force` on
  `WindowArgs`, `PlaceArgs`, or `OpenArgs` overrides), or
  `ShellAnswerMalformedError` (a success status with a body that is not the
  answer).
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
- `workspace_layout.transport` and `workspace_layout.agent_identity`: the
  standard-library halves of the client: `exchange_with_shell` (one request,
  answered as a status and a JSON object or text), `quote_answer`,
  `refusal_detail`, and `chat_id_from_environment()`. The command uses these
  instead of the client, so it imports no pydantic.
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

`uv run --no-sync workspace-layout <subcommand>`, from the repo root, is how an agent
reads and arranges the desktop; the `manage-desktop` skill is its guide and
`uv run --no-sync workspace-layout --help` its reference. `--no-sync` runs the script the
workspace build installed without uv first checking the lock and the venv: that check
costs every call tens of milliseconds, and when the lock and a `pyproject.toml` disagree it
relocks and syncs before the command runs. Every subcommand posts one op
to the shell's op route under the calling agent's own chat as the requester
(`MINDS_CHAT_ID`, else `MNGR_AGENT_ID`), except `desktops` and `list`, which
read `GET /api/inventory`; `list` gives each app the `link` to write for it, its
`share_url` once the workspace has been shared, else its port-less `http://<label>.localhost/`.
Descriptions go to stderr; stdout carries only a window id (`open`, `show`), the
JSON of the read commands, and a desktop's shortcuts after a `shortcut` write.
Exit codes are `0` (done), `1` (refused or unreachable), `3` (the shell or an
app cannot act right now: retry), and `4` (the window is popped out into its own
window: pass `--force`, or leave it to the user). Every mutating subcommand
takes `--force`, and only the ones whose arguments take it send it: `minimize`,
`restore`, `maximize`, `place`, and `open`, which it lets move a popped-out
window, and `focus` and `close`, which ignore it. A summary notes when an op
raised a popped-out window in its own window, brought one back, left an
`open --beside` unpaired, or landed for a client with no desktop window open.

`show <app> --path P [--showing P ...] [--repoint PAGE ...]` runs the shell's
`show` op: it raises a window already at the path (or at a `--showing` path),
else points an on-screen window on one of the `--repoint` pages at it, else the
app's pinned window, else opens one.

The command imports only the standard library and this library's
dependency-free modules (`transport`, `agent_identity`, `shell_url`, `errors`,
and `app_manifest.registry_location`), because an agent runs it once per layout
action and importing pydantic and building the models was most of each call's
time. It posts each op's arguments as the wire spells them and reads answers as
plain JSON. The shell reads every body with the request models in `ops`, so a
value off its rule (an app name, a window, a path, a frame, a cell, a client id)
is refused with a 400 that names the argument, and the command exits 1 with
that line. The command checks only what no model owns: the retired spellings
and verbs, flags that exclude each other, and the text forms it turns into
records (`--frame`, `--cell`, `--param`). `place` takes `--state snapped-left|snapped-right|maximized` (the
`WindowState` the op sets, which its description names: `as snapped-left`) or
`--frame x,y,width,height`; the retired `--zone` is refused with that form.

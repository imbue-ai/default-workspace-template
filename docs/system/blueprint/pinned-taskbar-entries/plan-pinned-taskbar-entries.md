# Pinned taskbar entries and the chat avatar

This is the spec for three additions to the desktop interface: a pinned taskbar entry that an app declares in its manifest and that stands for one window of that app on every desktop, a per-client presentation of that entry that can pop it out of the bar as an always-on-top avatar, and a per-window location scope that lets such a window keep a separate path for every client.
Together they give the chat app a floating creature in the corner of the desktop whose click opens and hides the chat, whose image says whether any agent is working, and whose chooser lets the workspace pick its design, without the shell learning anything about chats or agents.
It is written for the people and agents implementing it, and it is the reference the implementation is judged against.
It builds on the desktop interface V1 ([plan-desktop-interface.md](../desktop-interface/plan-desktop-interface.md), [concepts.md](../desktop-interface/concepts.md), [contracts.md](../desktop-interface/contracts.md)) and amends those documents where section 9 says.
The prototype it draws its art and its chooser from is Kanjun's `desktop-mode` template, whose pet subsystem V1 deliberately left out.

## 1. Purpose and principles

The desktop's taskbar has one entry per window of the active desktop, and pressing an entry restores, minimizes, or raises its window.
The chat avatar is that entry, made permanent and made fun: it is always present, whether or not a chat window has been opened yet, it can float above the windows instead of sitting in the bar, and it draws itself as a creature whose posture follows the agents.
Everything it does is something the taskbar already does.
Nothing in this design is a new kind of thing on the desktop.

The five principles of the workspace app model hold: one owner per fact; the shell is generic and names no app; truth is shared while arrangement is scoped; minimal shell state; no two-phase commits.
The two principles of the desktop V1 hold with one amendment each:

- **The URL is the state**, and every client follows it, except for a window whose location scope is *independent*, where each client follows its own path (section 3.3).
- **Rendering never rewrites truth.** Compact mode forces a floating entry back into the bar without writing its mode, as it forces a window maximized without writing its state.

Two constraints of the shell package bind every part of this design and are enforced by its ratchets: the shell imports nothing from mngr and never runs the `mngr` binary, and a literal app name never appears in the shell's code.
The avatar's knowledge of agents therefore comes from reading a file mngr writes, with plain JSON parsing, and the chat is named only in its own manifest.

## 2. Glossary

| Term | Meaning |
|---|---|
| Pin | An app's manifest declaration that it has a pinned entry: the path its pinned window opens, the entry's style, its location scope, and its default mode |
| Pinned window | The one window of a pinned app on a given desktop; shared, permanent, never closed |
| Pinned entry | The taskbar entry of a pinned window; present on every desktop because the window is |
| Entry mode | Where one client shows a pinned entry: `bar` or `floating`; per client |
| Entry style | How one client draws a pinned entry: `plain` (icon and title) or the style the pin declares (`avatar`); per client |
| Location scope | Whether a window's path and title are followed by every client (`linked`, the V1 rule) or kept by each client for itself (`independent`) |
| Home path | The path a pin declares; the shared path of an independent pinned window, and where a client with no path of its own opens it |
| Avatar | The `avatar` entry style: a creature drawn from a design, wearing a mood |
| Design | One SVG drawing of the avatar on a 100 by 100 grid, animated by mood; seven ship, more can be registered |
| Mood | What the avatar's image expresses: `working` or `idle` |

The desktop's `shared` and `personal` sharing modes keep their V1 meaning, who may open and close windows on a desktop, and are unrelated to a window's location scope.
The two pairs of words are never mixed.

## 3. The model

### 3.1 Pins

A pin is declared in an app's manifest as a `[pin]` table and copied onto the app's registry row by `forward_port.py`, the way `default_shortcut` and `launch_paths` are.
The set of pinned apps is exactly the set of registered, non-internal apps whose row carries a pin.
Nothing stores the set: an app is pinned while its manifest says so, and unpinned when the declaration is removed.
A user who dislikes an app's pinned entry chooses its `plain` style and `bar` mode, which makes it an ordinary-looking entry that is always there.

The table:

| Field | Type | Required | Default | Rule |
|---|---|---|---|---|
| `path` | string | yes | | The home path: a path under the app's origin obeying the launch-path rule (one leading slash, no query string, path characters only). It must be a page that is safe to open any number of times, since the pinned window is created at it without a launch path and without settling |
| `style` | string | no | `plain` | `plain` or `avatar`; the entry styles the shell ships |
| `scope` | string | no | `linked` | `linked` or `independent`; the location scope of the pinned window |
| `default_mode` | string | no | `bar` | `bar` or `floating`; the entry mode a client that has chosen none renders |

The chat's manifest declares `path = "/"`, `style = "avatar"`, `scope = "independent"`, `default_mode = "floating"` (section 8).

### 3.2 Pinned windows

Every desktop holds exactly one pinned window per pinned app.
A pinned window is an ordinary window record, `{id, app, path, title, opened_at, is_settling, is_pinned, scope}`, with `is_pinned` true, `path` the home path, `title` empty, `is_settling` false, and `scope` the pin's.
It is shared like every window, and it has the same id for every client.
Both new fields are additive with defaults, `is_pinned = false` and `scope = "linked"`, so a V1 `desktops.json` reads unchanged and the file keeps version 1.

Pinned windows are never closed.
The close route answers `409` for one, the agent `close` op is refused with the fix named (minimize instead), the close control is absent from its title bar, and Close is absent from its window menu and its entry's context menu.
The minds close chord, which closes the focused window, minimizes a pinned one instead.
Deleting a desktop still deletes its pinned windows with it.

The shell reconciles pinned windows on read, extending the rule that creates the `Home` desktop.
Every read of the desktops after the inventory has read the registry ensures that each desktop holds one pinned window per pinned app: it adopts the earliest-opened window of that app whose path equals the home path, marking it pinned, setting its scope, and clearing its shared title when the scope is independent, and creates one when there is none.
A write happens only when something was missing, and it broadcasts `desktops_updated` once, after the read that made it, never from inside it.
Desktop creation runs the same ensure before answering, so a new desktop is born with its pinned windows.
An app registered later is reconciled on the next read after the registry change.
When a pin is withdrawn, its windows stay as ordinary windows, `is_pinned` false, and can be closed like any other.

A pinned window has no placement in a client's layout until the client touches it, so by the V1 rule it reads as minimized with a cascaded frame.
A fresh client sees every pinned entry, all of them "closed", and the first press restores the window at the cascade frame.
No default frame beyond the cascade is given in this version.

### 3.3 Location scope

A window's location scope is `linked` or `independent`.

A **linked** window is the V1 window: one shared path and title, reported by whichever page drives it, stored on the window record, broadcast, and followed by every other client.

An **independent** window has a shared path that never changes, its home path, and a path and title per client.
When a page of an independent window reports its location, the shell stores the report under the reporting client in that client's window-paths file (section 5.1) rather than on the window record, and announces it to that client's windows alone.
A client that has no stored path for an independent window opens it at the home path.
The following rule of V1 section 4.6 runs against the client's own stored path for an independent window: the report the driving page made is already stored there, so nothing bounces, and the only things that move an independent page are an agent's `navigate` (which targets one client) and a reconnect that finds a newer stored path.
The taskbar entry, the launcher row, and the Running apps popover show the client's own title for an independent window, falling back to the app's display name.

The home path never changing has a side effect worth stating: an open of the app at the home path with the default `focus` behaviour finds the pinned window and raises it rather than opening a second root.

Only a pin sets a window's scope in this version.
The field is stored on the window so a later toggle needs no migration.

### 3.4 Entry presentation, per client

How one client shows a pinned entry is three values on the client record, keyed by app: the mode (`bar` or `floating`), the style (`plain` or the pin's declared style), and the floating position.
They are global to the client rather than per desktop: an always-on-top entry that jumped when the desktop switched would read as broken.
A client with no presentation for an app renders the pin's `default_mode`, the pin's declared style, and the default floating position.

The floating position is a point in fractions of the backdrop, the top-left corner of the entry's box.
It is written once at the end of a drag and clamped at render time so the whole box stays inside the backdrop, above the taskbar.
The default is the bottom-right corner, inset by the theme's tokens.

In compact mode every pinned entry renders in the bar whatever its mode says, and the mode is not rewritten.

### 3.5 The avatar

The `avatar` style draws a design wearing a mood.

**Designs** are the prototype's pets, renamed: seven bundled SVGs on a 100 by 100 grid, animated by a small shared stylesheet through a fixed vocabulary of classes, plus a bounded catalog of designs registered from inside the workspace.
A registered design is validated as passive drawing markup with the prototype's validator (an allowlist of elements and attributes, local gradient paints only, no scripts, styles beyond the shared sheet, links, images, or foreign content, at most 256 KiB, at most 32 custom designs), and its original is kept verbatim under the app data directory.
The shell renders an isolated image per design and mood, injecting the mood as a data attribute and the shared stylesheet, and serves it with a restrictive content security policy, as the prototype does.
The prototype's per-design rest and busy expressions (closed eyes at rest, open eyes and a design-specific motion while working) come across with the assets.

**The selection** is one shared value for the workspace: the id of the design every client draws.
It lives in one small state file and is broadcast when it changes, so every window of every client changes together.
A selection naming a design that no longer exists draws the default design.
A per-user override of the workspace's choice is deferred (section 12).

**The mood** is `working` or `idle`, derived from the agents on the machine (section 4.6).
There is no badge dot and no third image state in this version.

### 3.6 Ownership

| Fact or verb | Owner |
|---|---|
| Whether an app is pinned; its home path, style, scope, default mode | The app's manifest, through the registry |
| The pinned window on each desktop: its id and existence | Shell, shared, reconciled on read |
| A linked window's path and title | Shell, shared (V1) |
| An independent window's path and title | Shell, per client |
| A pinned window's frame, state, minimized flag, stack position | Shell, per client (a V1 placement) |
| A pinned entry's mode, style, and floating position | Shell, per client, on the client record |
| The avatar designs and the workspace's selected design | Shell, shared |
| The mood | Derived in the shell from mngr's observe events; never stored |
| Which windows have a pinned entry, and where each is drawn | Derived in the browser |

### 3.7 Invariants

- Every desktop holds exactly one pinned window per pinned app after any read of the desktops that followed a registry read.
- A pinned window is never removed by a close; only its desktop's deletion or its pin's withdrawal changes that.
- An independent window's shared path equals its home path at all times.
- A client's stored path for an independent window is written only by that client's own location report or by an agent op targeting that client.
- The taskbar shows one entry per window of the active desktop; a pinned window's entry is that entry, drawn in the bar or floating, never both and never twice.
- Nothing in the shell package names an app, imports mngr, or runs mngr.

## 4. Behaviour

### 4.1 The entry in the bar

A pinned entry in `bar` mode occupies its window's place among the taskbar entries, in opening order like every other.
In `plain` style it is the V1 entry: icon and title, dimmed while minimized, marked while focused.
In `avatar` style the app's icon is replaced by the avatar image at entry size, wearing the current mood, beside the title; in compact mode the image alone.
Dimming and the focused mark apply in both styles.

### 4.2 The floating entry

A pinned entry in `floating` mode leaves the bar and is drawn in a layer above every window and every live page, below the launcher overlay, the floating menus, and the modals.
The layer is a sibling of the windows layer inside the backdrop's own stacking context, at the theme's sticky level, which is what the snap preview and the shortcut ghost already use to sit above the windows.
The layer is inert; only the entry's own box takes a press, so nothing else on the desktop is shadowed.
The entry is a square button of the theme's floating-entry size at the client's stored position, drawing the avatar image, or in `plain` style the app's icon on a raised tile.
It shows whether its window is shown or minimized the way the bar entry does, through `aria-pressed` and a dim state.

Dragging the entry moves it.
The gesture layer binds it by its data attribute, with the same threshold, pointer capture, and click suppression as a window drag: a press that never travels the threshold is a click, and a drag that began fires no click.
During the drag the entry follows the pointer, clamped inside the backdrop; on release the position is written to the client record, once.
A long press opens its context menu, as it does for shortcuts and bar entries.

In compact mode the floating layer is empty and every pinned entry is in the bar.

### 4.3 Clicking

A press on a pinned entry, in either mode, is exactly the taskbar entry click of V1 section 4.10: restore and raise the window when minimized, minimize it when it is the focused window, raise it otherwise.
There is no "open" step, because the window always exists.
The first restore of a window the client has never placed lands at the cascade frame.

### 4.4 Menus

The pinned entry's context menu is the taskbar entry menu with Close removed and the presentation verbs added, so it stays consistent with every other entry:

1. Restore or Minimize
2. Maximize or Restore size (absent in compact mode)
3. divider
4. Float or Move to taskbar (absent in compact mode)
5. Show as plain entry or Show as <style>, when the pin declares a style
6. Change avatar..., when the current style is `avatar`

The pinned window's own menu is the V1 window menu without Close: Refresh, Share, Stop or Start.
Its title bar has minimize and maximize but no close control.
There are no keyboard shortcuts for any of this in this version.

### 4.5 Independent windows in the browser

For an independent window the client's live page is created at the client's stored path, else the home path, and the frame's title and the entry's title come from the client's stored title.
The page's location reports go to the location route with the client id; the shell stores them per client and answers the effective record, and the store applies that answer the way it applies a linked window's.
The following step compares each independent page's last report against the client's stored path, which the store carries beside the layout, so a report from this client never navigates its own page, and a `navigate` op for this client does.
Because that stored path arrives with the layout rather than with the desktops, the following step runs after a layout load as well as after a desktops update; for linked windows a layout load changes nothing it compares.

### 4.6 The mood

The mood is computed by one backend module that reads the agents event file the mngr observer writes:

```
$MNGR_HOST_DIR/events/mngr/agents/events.jsonl
```

`MNGR_HOST_DIR` is inherited from the bootstrap shell like every service's environment; when it is unset the reader falls back to `~/.mngr`, as the chat app does.
The observer is the one the chat app runs inside the workspace.
The file is append-only JSONL under the standard event envelope: a full snapshot of every agent every five minutes (`AGENTS_FULL_STATE`), one agent's state whenever its host shows activity (`AGENT_STATE`), and a removal when an agent is destroyed (`AGENT_REMOVED`).
The module reads from the end of the file back to the most recent full snapshot, applies every later state and removal event on top, and drops any agent whose labels carry `is_primary` equal to `true` (the workspace's services agent).
From each remaining agent it reads only `id`, `state`, and `labels`; unknown fields are ignored, and a line that fails to parse is skipped with a warning.

The mood is `working` when any remaining agent's state is `RUNNING` or `RUNNING_UNKNOWN_AGENT_TYPE`, and `idle` otherwise, including when there are no agents and when the file does not exist.

Staleness is the age of the newest line's timestamp.
Past ten minutes (twice the snapshot interval) the status is marked stale; the image keeps its mood and the entry's tooltip says the status may be out of date.
A missing file is stale from the start.

The module watches the file with the shell's existing file-system observer, debounces a burst of writes, refolds, and pushes one `avatar_status` message to every window when the mood or the staleness changes.
Every window receives the current status on connect.
No route serves it; the frontend keeps the last message in the store.

The module lives in the avatar package and imports nothing from mngr.
The conventions it relies on are duplicated as its constants: the file path above, the three event type names, the `is_primary` label key, and the two lifecycle state names that mean working.
Each is a stable, documented mngr convention; a change to any of them shows up as a stale or idle avatar, never as an error.

### 4.7 The chooser

"Change avatar..." opens a dialog on the shared Modal: a grid of every design's still image (the `preview` rendering, which suppresses animation), the selected one marked, a "Design your own..." button, and a link to the selected design's original SVG.
Choosing a design writes the workspace's selection and every window follows the broadcast.
The dialog closes on Escape, on a press outside, or on Done.

"Design your own..." starts a chat seeded with the prototype's design prompt through the launch path that declares a `message` parameter, the way the launcher's "Start something" intents do, so the shell names no app.
The prompt tells the agent to draw a design, show a preview, and register it only after the user approves.
The button is disabled, with the reason as its tooltip, when no app declares such a launch path.
A note beside it says the message is sent when the chat opens; the prototype's editable draft is deferred.

Registration is the prototype's loopback-only route: an agent inside the workspace posts a design's id, label, SVG, and source path, the shell validates and stores it, and it appears in the chooser.
A bundled design's id cannot be replaced.
The `register_avatar.py` helper (the prototype's `pet_register.py`) posts a file and optionally selects it.

### 4.8 First visit, new desktops, upgrades

A new client sees every pinned entry in the pin's default mode and style, each showing its window as minimized.
A new desktop is created with its pinned windows.
A workspace upgraded to this version gets a pinned window on every existing desktop on the first read after the registry is read: an existing chat root window at `/` is adopted, otherwise one is created beside whatever chat windows the desktop already holds.
A chat root that had drifted to a chat-specific path is not adopted; the desktop briefly holds it and the new pinned window, and closing the old one resolves it.

### 4.9 Compact and touch

Compact mode renders every pinned entry in the bar in its style, image only for `avatar`, and hides the floating verb from the menu.
Touch mode uses long press for the menus, as elsewhere; the floating entry has the theme's touch target size at minimum.

## 5. The shell backend

### 5.1 State files

- `desktops.json` (version 1, unchanged): each window gains `is_pinned` and `scope`, both with defaults.
- `clients.json` (version 2, unchanged): each client gains `entries`, a map from app name to `{mode, style, position}`, with `position` `{x, y}` in fractions or `null`; absent means the defaults.
- `window_paths/<client_id>.json`, new: `{"version": 1, "windows": {"<window_id>": {"path", "title"}}}`, the client's paths and titles for independent windows.
  Entries naming a window no longer on any desktop are dropped on read; the file is deleted with the client's layouts when the client is pruned.
- `avatar_selection.json`, new: `{"version": 1, "design": "<id>"}`; absent means the default design.
- `data/.apps/system_interface/avatars/catalog.json`, new: the registered designs with their originals, the prototype's catalog format, under the app data directory because the originals are the user's.

### 5.2 The manifest and the registry

`app_manifest` gains the `[pin]` table of section 3.1 with the validation rules given there, and `RegistryRow` gains `pin`.
`forward_port.py` copies the table onto the row the way it copies `default_shortcut`, refusing a malformed one with the same shape of error.
The `app` object of the inventory and of `apps_updated` carries `pin` (the table, or `null`).

### 5.3 Routes and messages

New or changed routes:

| Route | Change |
|---|---|
| `POST /api/desktops/<id>/windows/<window>/close` | `409` for a pinned window |
| `POST /api/desktops/<id>/windows/<window>/location` | body gains `client_id`; for an independent window the report is stored per client and the answer's `path` and `title` are the client's |
| `GET /api/placements/<desktop>?client=` | the layout answer gains `window_paths`, the client's stored `{path, title}` by window id for the desktop's independent windows; the save body carries no such field |
| `POST /api/clients/<client>/entries/<app>` | new: `{mode, style, position}`; answers the client record |
| `GET /api/avatars` | new: `{designs: [{id, label, source_path}], selected, default}` |
| `POST /api/avatars` | new, loopback only: register a design |
| `GET /api/avatars/<id>/image.svg?mood=&preview=` | new: the rendered image |
| `GET /api/avatars/<id>/source.svg` | new: the original |
| `POST /api/avatar-selection` | new: `{design}`; writes the selection and broadcasts |

New socket messages, all to every window unless said:

| Type | Payload | When |
|---|---|---|
| `avatar_status` | `{mood, is_stale}` | on connect, and when either changes |
| `avatar_selection_changed` | `{design}` | after the selection is written |
| `client_entries_changed` | `{client_id, entries}` | to that client's windows, after its presentation is written |

A stored window path is announced with the existing `placements_updated` for that client and desktop, carrying a save id the shell minted, so the client's other windows refetch the layout and take the new path with it.
The layout file's own stamp is untouched by that write, so a browser save in flight is not made stale.

### 5.4 The pure editor and the state

`desktop_document.py` gains: `pinned_window(app, home_path, scope)` (the record an ensure creates), `with_pinned_windows_ensured(desktop, pins)` (adopt or create per pin, answering the desktop and whether it changed), and `without_pin_marks(desktop, unpinned_apps)`.
`ShellState.list_desktops()` runs the ensure after the default-desktop rule, under the state lock, writing and broadcasting only on change.
`ShellState.close_window()` raises `PinnedWindowError` (a `409`) for a pinned window.
`ShellState.report_window_location()` branches on the window's scope.
A `WindowPathStore` owns the per-client files; `ClientStore` gains the entries; an `AvatarSelectionStore` owns the selection.

### 5.5 Agent ops

- `close` on a pinned window is refused with "the window is pinned; minimize it instead".
- `navigate` on an independent window writes the target client's stored path and announces it to that client; on a linked window it is unchanged.
- `desktops` and `list` show `is_pinned` and `scope` on every window; an independent window's `path` is its home path, which the `manage-desktop` skill explains.
- No op pins, unpins, or changes presentation; those are the manifest's and the user's.

## 6. The frontend

### 6.1 Modules

Following the V1 layering (theme, model, geometry, reducers, store, pages, gestures, views):

- `model/records.ts`: `is_pinned` and `scope` on `WindowRecord`; `pin` on `AppRecord`; `entries` on `ClientRecord`; `window_paths` on `Layout`; the parsers, refusing a malformed document as V1 does.
- `model/api.ts`: the routes of 5.3.
- `geometry/floating.ts`: the clamp of a floating position into the backdrop and the default position, pure.
- `reducers/desktopState.ts`: `taskbarEntries` gains `isPinned`, the effective title of an independent window, and the entry's effective presentation; `barEntries` and `floatingEntries` split the entries by effective mode and compact mode; `avatarStatus`, `avatarSelection`, and `entries` on the state with their events.
- `store/DesktopStore.ts`: the presentation write, the drag of a floating entry (begin, update, end), `closeFocusedWindow` minimizing a pinned window, and the new socket handlers.
- `gestures/pointerGestures.ts`: a `floating-entry` binding by `data-pinned-entry`.
- `views/FloatingEntries.ts`: the layer and its entries.
- `views/TaskbarEntry.ts`: the `avatar` style.
- `views/AvatarChooserDialog.ts`: the chooser.
- `views/WindowMenu.ts`: the pinned variants of the window and entry menus.

### 6.2 The avatar image

An avatar image is an `<img>` whose `src` is the image route with the current design and mood, so a mood change is one attribute change and the browser's image isolation keeps the SVG passive.
A failed load falls back to the default design at the same mood, as the prototype does.

## 7. Exact shapes

### 7.1 Manifest

```toml
[pin]
path = "/"
style = "avatar"
scope = "independent"
default_mode = "floating"
```

### 7.2 Registry row and inventory `app`

`"pin": {"path": "/", "style": "avatar", "scope": "independent", "default_mode": "floating"}` or `"pin": null`.

### 7.3 Window

`{"id", "app", "path", "title", "opened_at", "is_settling", "is_pinned": false, "scope": "linked"}`; the two new fields default when absent.

### 7.4 Client record (wire and file)

`{"id", "active_desktop", "last_seen", "is_connected", "entries": {"<app>": {"mode": "bar" | "floating", "style": "plain" | "avatar", "position": {"x": 0.9, "y": 0.85} | null}}}`.

### 7.5 Selectors shared with tests

| Attribute | On |
|---|---|
| `data-pinned-entry="<app>"`, `data-entry-mode="bar\|floating"`, `data-entry-style="plain\|avatar"` | each pinned entry, in the bar or floating |
| `data-floating-entries` | the floating layer |
| `data-mood="working\|idle"`, `data-stale="true\|false"` | each avatar image's button |
| `data-avatar-chooser`, `data-avatar-design="<id>"` | the chooser and its cells |
| `data-menu-item="float\|move-to-taskbar\|style-plain\|style-avatar\|change-avatar"` | the menu rows |

### 7.6 Theme tokens

`--desk-floating-entry-size: 56px`, `--desk-floating-entry-inset-x: 16px`, `--desk-floating-entry-inset-y: 12px`; read into the metrics record with the others.
Colours and radii come from the existing tokens.

## 8. The chat app

The chat manifest gains the `[pin]` table of 7.1.
Nothing else in the chat app changes: its root page keeps reporting the selected chat as its location, and because the pinned window is independent, that report is stored for the reporting client alone.

## 9. Amendments to the desktop interface documents

Landed with the implementation:

- concepts.md 2.2: path and title are shared for a linked window; an independent window keeps them per client, with the home path shared.
- concepts.md 2.7: a pinned window's title bar has no close control.
- concepts.md 2.8 and plan 4.10: the taskbar shows one entry per window; a pinned window's entry may be drawn in the bar with a style or floating above the windows.
- concepts.md decision 6 gains the same sentence; decision 14 names the independent scope as its exception.
- plan 4.5: close is refused for a pinned window.
- plan 8 and contracts.md 2, 3, 4, 5, 6, 11, 12: the manifest table, the row field, the files, the routes, the messages, the tokens, and the selectors above.
- plan 15: "presence, avatars" is narrowed; the avatar is here, presence is not.
- The `manage-desktop` skill: pinned windows cannot be closed, `navigate` on an independent window moves one client, and the listing's `is_pinned` and `scope`.
- The `build-app` skill's manifest notes: the `[pin]` table.
- The minds design doc's "interact with mngr exclusively through the CLI" gains the one exception this design makes, reading the observer's event file, and why.

## 10. Testing

Unit tests, beside the code:

- The manifest table and its refusals; the registry copy; the inventory `app` field.
- The ensure: a fresh desktop, an adopted window, a withdrawn pin, an app registered late, a desktop created after the pin.
- Close refusal on every path (route, op, chord).
- Independent scope: a report stored per client, the home path unchanged, the layout answer's `window_paths`, pruning with the client, `navigate` on one client.
- The status fold against a fixture events file: a snapshot, later states, a removal, the primary agent dropped, a malformed line skipped, an absent file, staleness.
- The designs validator and renderer, ported with the prototype's tests.
- Reducers: bar and floating splits in both modes, the effective presentation, the effective title, the floating clamp.

End to end, in `test_e2e.py` with a stub app declaring a pin:

- Every desktop holds the pinned window; a new desktop gets one; the taskbar shows its entry and no plain entry beside it.
- Click restores, minimizes, raises; the floating entry drags and its position survives a reload; the menu floats and returns it to the bar; plain style shows the icon.
- Two clients on one desktop with an independent pinned window each report a path and neither follows the other; a linked stub window still follows.
- An agent `navigate` moves one client's page; an agent `close` is refused.
- A phone viewport shows the entry in the bar.
- The chooser lists the designs, selecting one changes every open window, and the image URL carries the mood a fixture status file produces.

## 11. Delivery

This is one pull request.
The work is small: a manifest table copied onto the registry row, two defaulted fields on the window record, one ensure function on the read path, a refused close, one per-client file with a branch in the location route, a three-field presentation map on the client record, a floating layer with one gesture binding and a few menu rows, and the avatar package, most of which is ported from the prototype.

A suggested commit order inside that pull request, each commit leaving the repository green:

1. The pin, the pinned window, and the refused close, with the chat manifest declaring `style = "plain"` so the entry is visible at once.
2. The independent location scope, on its own because it touches the following rule and the live pages, and with its two-client test.
3. The per-client presentation, the floating layer, and the menu rows.
4. The avatar package, the status reader, the chooser, and the chat manifest's switch to `style = "avatar"`, `default_mode = "floating"`.
5. The document amendments of section 9 and the end-to-end scenarios of section 10.

## 12. Deferred

- A per-user override of the workspace's selected design; this version has the shared default set only.
- Pinning and unpinning by the user or by an agent; the set is the manifests'.
- A user toggle of a window's location scope; only a pin sets it.
- A default frame for a pinned window anchored above its floating entry; the cascade is used.
- Attention and error expressed on the avatar, and the prototype's badge dot, which stays removed.
- The prototype's `listening` mood while the chat is open.
- An editable draft for "Design your own..." (a `draft` launch parameter); the message is sent on open.
- Keyboard shortcuts for the entry and the chooser.
- Showing a client's stored paths for independent windows in the agent listing.
- A workspace-owned observer service, so the mood no longer depends on the chat app's process.

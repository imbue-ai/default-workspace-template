# The launcher menu and the Getting Started app

This is the spec for reshaping the desktop's launcher and for moving its "Start something" and "Start from a template" content into an app of its own.
The launcher stays the text field at the taskbar's left, but the panel it opens becomes a compact menu of rows in which something is always highlighted: Enter runs the highlighted row, and when nothing matches what was typed, Enter starts a new chat with the typed text and Ctrl+Enter sends it to an existing chat.
Both land in the chat window the desktop already has, the pinned one, rather than opening another.
The intents and the template catalog become the Getting Started app, an ordinary registered app that opens its own window once per workspace, for the first client to connect, beside the welcome chat.

It is written for the people and agents implementing it, and it is the reference the implementation is judged against.
It builds on the desktop interface V1 ([plan](../desktop-interface/plan-desktop-interface.md), [concepts](../desktop-interface/concepts.md), [contracts](../desktop-interface/contracts.md)), on the pinned windows of the [pinned-taskbar-entries plan](../pinned-taskbar-entries/plan-pinned-taskbar-entries.md), and on the shortcut rules of the [window-bound-resources spec](../../specs/window-bound-resources.md), and amends those documents where section 12 says.
The launcher's interaction model follows Gleb's `companion-desktop` prototype; its look does not.

## 1. Purpose and principles

The launcher today opens the old New Tab page above the taskbar: a full-width panel of launch-path tiles, window rows, eight intent tiles, and the template shelves.
It is a page to read, not a control to type into, and nothing typed into it can be sent anywhere.
The prototype's launcher is the opposite: one field, a short menu of rows above it, and the rule that whatever is typed can always be sent as a chat.

Three things are settled by this design:

1. **The field is a command line for the desktop.** Something in the menu is always highlighted; Enter runs it; the two free-text rows at the foot of the menu catch everything that is not a match.
2. **Starting a chat never opens a second chat window.** The chat's pinned window exists on every desktop; a new chat or a sent message is a navigation of this client's view of that window, so one click makes one chat and the window the user already knows is the one that shows it.
3. **The shell offers nothing of its own to start.** The intents and the templates are content, and content belongs to an app.
   Getting Started is that app, and the shell learns about it the way it learns about every app: from its manifest.

The principles of the workspace app model hold: one owner per fact; the shell is generic and names no app; truth is shared while arrangement is scoped; minimal shell state; no two-phase commits.
Two consequences bind every part below.
The shell never names the chat: which launch path takes typed text, and which app has a window to send it to, are read from manifests and from the desktop.
The Getting Started page never names the chat either: it asks the shell to start a chat with a text, through one new contract message, and the shell picks the app.

## 2. Glossary

| Term | Meaning |
|---|---|
| Launcher | The text field at the taskbar's left and the menu it opens (concepts.md 2.8, "overlay" there) |
| Menu | The panel above the field: a column of rows, anchored to the field, never a window |
| Row | One thing the menu can run: a launch-path row, a window row, or a free-text row |
| Launch-path row | A row that runs one launch path of one app, as a tile did |
| Window row | A row that shows an existing window, on this or another desktop |
| Free-text row | A row that runs a launch path with the field's text as one of its params; it accepts anything typed |
| Text param | The launch-path param the launcher fills with the field's text, declared in the manifest |
| Primary and secondary text action | The first and second free-text rows in order; Enter's fallthrough and Ctrl+Enter's target |
| Highlight | The one row Enter runs; always exactly one while the menu is open and non-empty |
| Getting Started | The app holding the intents and the template catalog |
| First-visit window | The window Getting Started opens for itself once per workspace, on the first desktop, for the first connected client; an ordinary window thereafter |

## 3. The model

### 3.1 Text params

A launch path may declare which of its params the launcher fills with typed text:

```toml
[[launch_paths]]
id = "new"
label = "New Chat"
path = "/new"
params = [
    {name = "account_id", label = "Provider account", required = false},
    {name = "message", label = "First message", required = false},
]
text_param = "message"
```

`text_param` is optional, names one of the launch path's declared params (a manifest naming an undeclared param fails to load), and marks the launch path as a free-text row.
A launch path without it never receives typed text, whatever its params are called; the chat root's `draft` param is not a text param, so the "Chat" row is never a free-text row.

The free-text rows of a machine are every launch path with a `text_param` of every registered, non-internal app, in the launcher's app order (`launcher_rank`, lowest first, then registry order) and within an app in manifest order.
The first is the primary text action, the second the secondary; any further ones are rows with no key binding.
On a stock machine the chat declares two: `new` ("New Chat", primary) and `send` ("Send to chat...", secondary; section 9).

### 3.2 Running a free-text row

Running a free-text row with text `t` means running its launch path through the shell's launch route with `t` as the value of its text (or draft) param (the post-launch-paths plan section 4.1), in **pinned-first** mode:

1. When the row's app has a pinned window on the active desktop, whatever its scope, the launch's target is that window: the shell resolves the page (for a POST launch path, by posting the text and its envelope to the app) and points the window at it as a location report from this client would (an independent window moves this client's page alone; a linked one moves everyone's, which is safe since the page is pure), and the window is restored and raised.
2. Otherwise a new window is opened at the page (target `new`).

A GET launch path's text is bounded by the window path: a path is at most 2048 characters (contracts section 1), so a free-text row whose page path would exceed that is disabled, with "Too long to send from here" as its tooltip and no key binding while it is disabled.
The bound is checked on the encoded path, not the typed length, since encoding can triple a non-ASCII text.
A POST launch path carries the text in a body and has no such bound.

Empty text runs the primary action with no text param at all (for the chat, an empty new chat) and disables the secondary action.

**Note:** the chat's free-text launch paths are POSTs (the post-launch-paths plan, which resolved issue imbue-ai/default-workspace-template#646): the shell makes the request once and the window only ever shows the pure page the app answered, so a reload runs nothing again and no idempotency key is needed.

A free-text row is never matched by the query, so typing `new chat` highlights no row and Enter starts a chat whose first message is "new chat".
The row's caption shows the text it will send (section 4.2), so what Enter does is on screen before it is pressed; this is accepted.

### 3.3 Running a launch-path row

A launch-path row runs its launch path in one of two modes, decided by the manifest rather than by the row:

- a launch path whose `path` equals its app's pin `path` runs in **focus** mode: the app's pinned window on the active desktop is restored and raised, and nothing is opened (the shell's own rule that a focus-mode open at the home path finds the pinned window);
- every other launch path runs in **new** mode, as its shortcut does: a new window at the path.

On a stock machine "Chat" raises the pinned chat window and "Terminal", "File Viewer", and "Browser" open windows.

### 3.4 The first-visit window

Getting Started opens its own window once per workspace, the way the chat app surfaces the welcome chat (`imbue/chat/auto_open.py`): from the app, through the loopback op route, for the first client that connects.
The shell learns nothing new; no manifest field, no shell state, and no placement rule are added for this.

On startup the app starts one background thread that, while the window has not yet been delivered, polls the shell's client list (`GET /api/clients`) and, once a connected client is listed, posts two ops targeting that client on the first desktop (the first entry of `GET /api/desktops`):

1. `open` with `app = getting-started`, `path = /`, `if_present = focus`, which answers the window id (an open of the same path twice answers the same window, so a retry is harmless);
2. `place` of that window with `frame = 0.07,0.05,0.38,0.9`, the left complement of the pinned frame (contracts 4.2), a hair short of it, which shows it normal at that frame on top of the client's stack.

Delivery is remembered in a ledger, `data/.state/getting-started/first_window.json` (`{"is_delivered": true}`), written once both ops were accepted; a restart of the app with the ledger present opens nothing, and closing the window never brings it back.
A shell that cannot be reached, or that refuses an op, is retried on the next poll for as long as the window is undelivered.
The window is ordinary from the moment it exists: closing it removes it for everyone, and a second client's first visit sees it minimized in its taskbar, as it sees any window opened elsewhere.

`x` clears the one-column shortcut grid on a wide backdrop (the grid inset plus one cell width, 112px, is under 0.07 of any backdrop 1600px or wider; a narrower backdrop overlaps the column's edge, which is accepted for the room the tiles gain).
The chat's welcome-chat restore and this open flush independently once a client connects, so which of the two windows ends on top is not fixed; the two frames do not overlap, so only the focus differs, which is accepted.

### 3.5 The launcher's rows

Given the inventory, the desktops, this client's layout, and the query `q`:

- **Launch-path rows:** one per launch path of every non-internal app, apps in launcher order, launch paths in manifest order, less every launch path that is a free-text row (those are listed once, at the foot).
- **Window rows:** every window of every desktop, the active desktop's first and in opening order, each read with this client's effective title (an independent window's own title, else the app's display name).
- **Free-text rows:** section 3.1, in order: the primary always, the rest only while there is text to send.

With `q` empty the menu shows the launch-path rows and the primary free-text row; window rows appear only while typing.
With `q` non-empty a launch-path row stays when every whitespace token of `q` occurs in its label or its app's display name or app name (`matchesQuery`, unchanged), a window row when every token occurs in its title, its desktop's name, or its app's names; free-text rows always stay.
A `q` with a line break in it is a message, not a query (section 4.1): the menu shows the free-text rows alone, with no launch-path or window rows and no "no match" note.
Rows keep their section order.

The **highlight** is the first launch-path or window row when any survives, else the primary text action.
Arrow keys move it through every enabled row, wrapping; hovering a row moves it there.
Enter runs the highlighted row; Ctrl+Enter (Cmd+Enter on macOS) runs the secondary text action whatever is highlighted; a click runs the clicked row.
Running any row closes the menu and clears the field.

### 3.6 The Getting Started app

An ordinary app under `system/apps/getting_started`, registered as `getting-started` ("Getting Started"), declaring no launch paths (so the shell synthesizes its one, `open` at `/`, labelled "Open Getting Started"; contracts section 2), a `default_shortcut` of `{launch = "open", mode = "focus"}` (one window is what it is for, as for the browser), `launcher_rank = 5`, `critical = false`, and the first-visit opener of section 3.4.
Its page holds, top to bottom: a search field over its own content; "Start something", the eight intent tiles of today's `startSomething.ts`, two to a row with four shown at first and the rest behind "See more"; "Start from a template", the catalog shelves of today's `TemplateShelves.ts`.
The page is its own scroller (the shared base styles pin the body to the viewport), so a window shorter than its content scrolls.
Picking a template shows the template's detail as a page inside the app (today's `TemplateDetailModal`, with a back control instead of a close), whose two actions are "Make it mine" and "Create a new machine from it".
Every tile and both actions start a chat with a seeded text through `shell:start-with-text` (section 3.7); the Getting Started window stays where it is and the chat comes up beside it.
The template catalog is the app's: today's `template_catalog.py` (the fetch, the six-hour reuse, the last-good copy on disk, `GET /api/templates-catalog`) moves into it whole, reading the same `SYSTEM_INTERFACE_TEMPLATE_CATALOG_URL` (kept so nothing outside the workspace changes) and keeping its cache under `data/.state/getting-started/`.

### 3.7 `shell:start-with-text`

One new page-to-shell message in the app contract:

| Direction | Type | Payload |
|---|---|---|
| page to shell | `shell:start-with-text` | `{"text"}`; the shell runs the primary text action with `text` (section 3.2) |

The shell accepts it only from a frame it created, like every page message, acts on the active desktop (a page can only be pressed there), and answers nothing.
When the machine has no free-text row the shell tells the user through its notification ("No app on this machine can start a chat"), so a page never has to know whether one exists.
`connectToShell` gains `startWithText(text)`, which sends it.

### 3.8 Ownership

| Fact or verb | Owner |
|---|---|
| Which launch paths take typed text, and which param | The app's manifest, through the registry |
| The order of the free-text rows | Derived: launcher app order, then manifest order |
| Whether the first-visit window has been opened; opening and placing it | The Getting Started app, in its ledger and through the op route |
| The first-visit window itself | Shell, shared, an ordinary window once opened |
| The launcher's rows, highlight, and query | Derived in the browser, transient |
| The intents, the template catalog, the detail page | The Getting Started app |
| Which chat a sent text goes to | The chat app, in its picker |

### 3.9 Invariants

- While the menu is open and has an enabled row, exactly one row is highlighted.
- Enter never starts a chat while a launch-path or window row is highlighted; it never does nothing while a free-text row is enabled.
- A free-text row never opens a second window of an app that has a pinned window on the active desktop.
- The shell package and its frontend name no app; the free-text rows and the pinned-first rule are read from manifests and windows.
- Getting Started opens its first-visit window at most once per workspace, and only onto the first desktop.
- The shell serves nothing about templates or intents.

## 4. Behaviour

### 4.1 The field

The field keeps its place at the taskbar's left and its width.
Its placeholder is `Start app or send message...`.
Focusing it opens the menu; typing filters; Escape clears the text, and a second Escape on an empty field closes the menu and blurs.
A press outside the field and the menu closes the menu and leaves the text; the next focus reopens it with the text still there.
The field is a one-row text area that grows with its text, to eight lines, and scrolls past that; it grows upward out of its one-row footprint in the taskbar, over the backdrop, so the taskbar and its entries hold still, and the menu rises with it.
Shift+Enter breaks the line: a longer message to an agent can be written here.
A text with a line break in it is a message rather than a query, and the menu offers the free-text rows alone (section 3.5); with the last line break gone the text is a query again.

In compact mode the field collapses to a button that expands over the taskbar's entries while the menu is open, as today.

### 4.2 The menu

The menu is a card anchored above the field's left edge (lifted by however far the field has grown past one row, so a message being written never runs under it), `--desk-launcher-menu-width` wide (a new theme token, 22rem, and 100% of the taskbar's width less its padding in compact mode), at most the backdrop's height less a margin, scrolling when longer, drawn with the desktop's surface, border, radius, and overlay shadow tokens.
Its rows are the taskbar entries' height, in the `--font-size-row` size: the app's glyph as a taskbar entry draws it for a launch-path or window row and the plus glyph for a free-text row, then the label, then a faint right-hand caption.
The launch-path rows come first, then a divider and the window rows while typing, then a divider and the free-text rows.
A section with no rows draws nothing, not a heading.
While typing with no launch-path or window match, one faint line above the free-text rows says `No apps or windows match`.

A launch-path row reads its launch path's label ("Terminal") with the app's display name as the caption when the two differ.
A window row reads the window's effective title with the app's display name as the caption, and the desktop's name after it for a window on another desktop; a minimized window's row is dimmed.
The highlighted row, whatever its kind, carries `Enter` as its last caption: the caption says what Enter does, and moves with the highlight.
The primary text action reads its launch path's label ("New Chat"); the secondary reads its label ("Send to chat...") with the platform's key (`Ctrl+Enter`, or `Cmd+Enter` on macOS) as a caption whatever is highlighted, since the chord runs it regardless; with text in the field the caption of either also carries the text's first words, faint, so the row says what it will do with what was typed.
A disabled free-text row is drawn faint with its reason as the tooltip.

The highlighted row wears the active fill and `data-highlighted="true"`.
The menu renders no loading state at any point; every row comes from state the browser already holds.

### 4.3 Running a row

| Row | What happens |
|---|---|
| Launch-path row whose path is its app's pin path | The pinned window on the active desktop is restored and raised |
| Any other launch-path row | A window opens at the launch path (`if_present: new`) |
| Window row on the active desktop | The window is restored and raised |
| Window row on another desktop | The client switches to that desktop, then the window is restored and raised |
| Free-text row, app pinned here | This client's view of the pinned window is pointed at the launch path with the text, then restored and raised |
| Free-text row, app not pinned here | A window opens at the launch path with the text |

The menu closes and the field is cleared after any of these.
A refusal from the shell is reported through the store's notification, as every other refusal is.

### 4.4 Starting a chat from Getting Started

A tile or a detail action posts `shell:start-with-text` with its seeded text.
The shell runs the primary text action with it (section 3.2): on a stock machine, the shell posts the chat's `new` launch path the text, the chat creates the chat and answers `/?chat=<id>`, this client's view of the pinned chat window goes there, and the window is restored and raised over Getting Started, whose own window is neither moved nor minimized.
The user reads the chat's first turn beside the tiles they came from.

### 4.5 Sending a text to an existing chat

Ctrl+Enter, or the "Send to chat..." row, runs the chat's `send` launch path with the text (section 8): the shell posts it to the chat's intake route, and the pinned chat window is pointed at the page it answers and restored.
With one chat to send to, the chat sends the text there at once and answers `/?chat=<id>`; with none it starts a new chat with the text, as `new` would.
Otherwise the chat holds the text as a pending intake and answers `/?intake=<token>`: the root shows a picker over its list, a typeahead over every chat's title; Enter or a click applies the intake to that chat (the chat sends the text and the root selects it, reporting `/?chat=<id>`); Escape dismisses the picker, discards the intake, and reports the selection alone, so the text is dropped.
Sending is the chat app's ordinary send, so a stopped chat is started by it as any send starts one.

### 4.6 First visit

A fresh workspace's first read after the registry seeds `Home` with its shortcuts and its pinned chat window.
The first client connects and fetches its layout of `Home` (never written), and is answered with the pinned chat window minimized at `PINNED_WINDOW_FRAME`.
The chat app's auto-open then navigates and restores the pinned window for that client, and the Getting Started app's opener opens and places its window for the same client (section 3.4): Getting Started on the left, the chat over the right half.
The two land in either order; both are op edits of the same client's layout under the shell's lock, so both placements survive whichever comes second.
A second client's first visit sees the pinned window minimized and the Getting Started window minimized in its taskbar; closing Getting Started on any client removes it for all.

### 4.7 Compact and touch

Compact mode shows the field as a button, the menu full width over the taskbar, and every window maximized as today.
Ctrl+Enter has no key on a touch keyboard; the secondary text action is a tappable row and needs none.
Long press replaces nothing here, since the menu has no context menus.

### 4.8 Keyboard

| Key | Where | Effect |
|---|---|---|
| Any character | field | Filters; the menu opens if closed |
| Down, Up | menu open | Moves the highlight, wrapping; with a line break in the text, moves the caret instead |
| Enter | menu open | Runs the highlighted row |
| Shift+Enter | field | Breaks the line; the text becomes a message and the menu keeps the free-text rows alone |
| Ctrl+Enter (Cmd+Enter on macOS) | menu open | Runs the secondary text action, when enabled |
| Escape | field | Clears the text; on an empty field, closes the menu |

## 5. The shell backend

### 5.1 Manifest and registry

- `app_manifest`: a `LaunchParamName` primitive (a non-empty string) types `LaunchParam.name`, and `LaunchPath` gains `text_param: LaunchParamName | None` (default None, validated against `params`); `RegistryLaunchPath` gains `text_param`.
- `forward_port.py` copies `text_param` into each launch path's row entry when the manifest declares one.
- The inventory's `app` object and `apps_updated` carry `text_param` on each launch path (`null` when absent).

### 5.2 State

- Nothing changes in the shell's state files or its placement rules: the first-visit window is an ordinary window the Getting Started app opens through the op route (section 3.4).
- `template_catalog.py`, its route, its config field, and its tests leave the shell.

### 5.3 Routes and messages

Removed from the shell: `GET /api/templates-catalog`.
Nothing else changes on the wire beyond the fields of 5.1 and the state of 5.2.

## 6. The frontend

### 6.1 Modules

Following the V1 layering:

- `model/records.ts`: `text_param` on `LaunchPath` and its parser.
- `model/search.ts` moves to `system/libs/workspace_ui/src/search.ts`: the launcher, the Getting Started page, and the chat's picker all match text the same way, so the shell imports it from the library like the other frontends.
- `model/launch.ts`: `freeTextRowsOf(apps)` (section 3.1's order), one classifier `launchRowKindOf(app, launchPath)` answering `text`, `focus`, or `new` in that order (a launch path with a `text_param` is a free-text row whatever its path; only then does a path equal to the pin's home path make a focus row), and `textPathOf(launchPath, text)` with the 2048 bound answered as a disabled reason; `promptTargetOfTiles` and `MESSAGE_PARAM` go, since the text param is declared.
- `reducers/launcherRows.ts`, new: the rows of section 3.5 and the highlight rule as pure functions over the state and the query, with `moveHighlight(rows, index, delta)`.
- `store/DesktopStore.ts`: `runFreeTextRow(row, text)` (pinned-first, over `navigateOwnWindow` and `restoreWindow`, else `openLaunchPath`), `runLaunchRow(row)` (focus or new by section 3.3), and the handler for `shell:start-with-text`, which is `runFreeTextRow` on the primary action.
- `views/LauncherField.ts`: the placeholder and the key handling of 4.8; the query and the highlight index stay transient state of `App.ts`, as the query is today.
- `views/LauncherMenu.ts`, new, replacing `LauncherOverlay.ts`: the card and its rows of 4.2.
- `relay.ts` and the contract module: `shell:start-with-text` and `startWithText`.
- `theme/default.css`: `--desk-launcher-menu-width` (22rem) and its compact value.
- Deleted from the shell's frontend: `LauncherOverlay.ts`, `startSomething.ts`, `TemplateShelves.ts`, `TemplateDetailModal.ts`, `TemplateArt.ts`, `hoverLift.ts`, and `model/TemplateCatalog.ts`, with their tests; the intents, shelves, detail, art, hover-lift, and catalog modules move into the Getting Started frontend.

### 6.2 Selectors

Data attributes, never classes (contracts section 12):

| Attribute | On |
|---|---|
| `data-launcher-field`, `data-launcher-overlay` | the field and the menu (the overlay's name is kept: the minds e2e runner and the template's own e2e suite find the menu by it) |
| `data-launcher-row="launch:<app>:<launch>" \| "window:<window-id>" \| "text:<app>:<launch>"` | each row |
| `data-launch="<app>:<launch>"` | each launch-path and free-text row (today's spelling kept) |
| `data-launcher-window="<window-id>"` | each window row (kept) |
| `data-text-action="primary" \| "secondary"` | the first two free-text rows |
| `data-highlighted="true" \| "false"` | each row |
| `data-disabled="true"` | a disabled free-text row |
| `data-key="enter"` | the `Enter` caption on the highlighted row |

The `.launcher-tile` class goes with the tiles.

## 7. The Getting Started app

- **Package:** `system/apps/getting_started`, a Flask app with its own `pyproject.toml`, installed as its own uv tool like the chat, run by a `[program:getting-started]` drop-in in `system/supervisord.conf.d/` that registers `system/apps/getting_started/app.toml` through `forward_port.py --manifest` and binds `127.0.0.1:8030` (free: the shell has 8000, the chat 8010, the browser 8081, the file viewer 8300, the terminal 7681 and 7683).
- **Manifest:** section 3.6, plus `priority = "getting-started"`, a new entry of `oom_priority.bands.SERVICE_BANDS` just above the file viewer's (a shed page costs one reload, so it is the most expendable built-in service), and an icon in the house style. `test_app_manifests.py` refuses a built-in in the `user` band, which is why the app gets a band of its own.
- **Routes:** `GET /` (the page), `GET /api/health`, `GET /api/templates-catalog` (moved from the shell), and `GET /_static/app_contract.js` from `SHELL_APP_CONTRACT_PATH`, as the terminal and the browser serve it.
- **First-visit opener:** the thread and ledger of section 3.4, started with the app and stopped with it.
- **Frontend:** a third member of the npm workspace, Mithril over `workspace_ui`, importing the contract module from its own origin; it connects to the shell on load, reports `/` with the title `Getting Started`, declares no navigation capability (it has one page, and a navigate reloads it), and posts `shell:start-with-text` from every tile and action.
  The search field at its top filters the intents by title and description and the templates by title, description, and author (today's `searchStartOptions` and `searchTemplates`), swapping the sections for results as the New Tab page did.
  The detail page is the template's drawing, write-up, requirements, and repository with a back control; its actions post the adopt message (`/use-template <url>`) and the create-machine message of today's `LauncherOverlay.ts`.
- **Catalog:** today's `template_catalog.py` and its tests move here unchanged in behaviour; the cache file moves to `data/.state/getting-started/template_catalog.json` (the state directory follows the registered name, as `data/.state/<name>/` does); the config reads `SYSTEM_INTERFACE_TEMPLATE_CATALOG_URL` with today's default, with a `CLEANUP:` note to rename the variable once the minds side sets a neutral one.
- **Build and apply:** `system/scripts/build_workspace.sh` already builds every npm workspace member and installs every app with a manifest and a pyproject, so it needs no change; the update apply's bundle list (`update_layout.FRONTEND_BUNDLES`) gains the app's bundle, so the apply installs and verifies it. No pre-flight boot is added: the chat's exists because the chat imports mngr and its plugins, and this app imports only Flask and httpx; the apply's post-restart health probes cover critical apps only, which this app is not, so a broken Getting Started is a window that does not load rather than a failed apply. `test_app_manifests.py` and contracts section 2's table gain its row.
- **Data:** nothing under `data/.apps/getting-started/`; the app persists nothing of the user's. Its machine state (the catalog cache and the first-visit ledger) lives under `data/.state/getting-started/`.

## 8. The chat app

As amended by the post-launch-paths plan, the chat's free-text launch paths are POSTs to one intake route:

- `new` has `text_param = "message"` and is a POST at `/api/chats/intake` with `presets = {target = "new_chat"}`.
- A second free-text launch path, in manifest order after `new`:

  ```toml
  [[launch_paths]]
  id = "send"
  label = "Send to chat..."
  path = "/api/chats/intake"
  method = "POST"
  params = [{name = "message", label = "Message to send", required = false}]
  presets = {target = "chat_selector"}
  text_param = "message"
  ```

- The intake route answers the page the shell opens: `/?chat=<id>` when the text was sent, or `/?intake=<token>` for the picker of 4.5, which the root shows on load and on `shell:navigate` before selection and finishes by applying the pending intake; on dismissal it discards the intake and reports the selection alone.
  A `send` with no text answers the root's path with nothing pending.
- The root serves `/` with no params of its own; a draft goes through the `draft` launch path (pinned-taskbar-entries plan section 4.7).
  The desktop shortcut still opens a second chat list (window-bound-resources decision 15), which this design leaves alone.

## 9. mngr-side changes

In the mngr repository, on the branch of the same name: the Electron e2e runner (`apps/minds/imbue/minds/desktop_client/e2e_workspace_runner.py`) finds the new-chat and new-terminal tiles by `.launcher-tile[data-launch=...]` and waits for a new chat frame after pressing the chat tile.
It changes to `[data-launch="chat:new"]` and `[data-launch="terminal:new"]` rows, expects the new chat inside the pinned window's root frame rather than in a new window, and waits for a Getting Started window on a fresh workspace's first visit beside the welcome chat.
`apps/minds/docs/design.md`'s "further chats start from the desktop's launcher" stays true; the workspace glossary gains Getting Started among the built-in apps.

## 10. Testing

- **Manifest and registry:** `text_param` validated against `params`, the registry copy, the inventory field.
- **Reducers:** the rows and the highlight of section 3.5 over fixtures: empty query, a matching launch path, a matching window on another desktop, no match (primary highlighted), a disabled secondary with empty text, the 2048 bound, wrap-around.
- **Store:** `runFreeTextRow` against the fake shell, pinned (a location write for this client and a restore) and unpinned (an open at the launch path); `runLaunchRow` focus and new; `shell:start-with-text` reaching the primary action.
- **End to end** (`test_e2e.py`, with a pinned stub app declaring a `text_param` on two launch paths): the menu opens with launch-path and free-text rows; typing filters and moves the highlight; Enter on a highlighted launch path opens its window; Enter with no match points the stub's pinned window at the text path and restores it; Ctrl+Enter runs the secondary; a phone viewport shows the menu full width and the free-text rows tappable.
- **Getting Started:** unit tests beside the moved catalog module and the opener (delivered once, held while no client is connected, retried after a refusal, the ledger read back); a frontend test per view; the routes serve the page, the health probe, the catalog, and the contract module.
- **Chat:** the `send` path's picker in `test_e2e.py`: pick sends and selects, Escape drops.
- **Ratchets:** the no-app-name rule holds (the shell reads `text_param` and pins, never `chat`); the pixel-metric rule holds through the new token.

## 11. Implementation order

Each step leaves the tree green; the whole is one pull request per repository.

1. `app_manifest`, `forward_port.py`, the inventory: `text_param`; the chat manifest's `text_param` on `new`.
2. The shell's frontend: the rows reducer, the store's two run methods, `LauncherMenu` replacing `LauncherOverlay`, the field's keys, the token, the selectors; the e2e launcher scenarios rewritten.
   The intents and templates leave the shell in this step, so the launcher is briefly without them until step 4 lands in the same pull request.
3. The contract message and its store handler.
4. The Getting Started app, with the catalog module and the intent, shelf, detail, and art modules moved in; build and apply plumbing; its manifest, its band, and its first-visit opener.
5. The chat's `send` launch path and picker.
6. Documents (section 12), changelog entries for `system_interface`, `chat`, `app_manifest`, `getting_started`, `system`, and `.agents`, and the mngr-side pull request.
7. Manual verification in a fresh dev workspace: the first visit's two windows; Enter, Ctrl+Enter, and a tile from a laptop and a phone.

## 12. Amendments to existing documents

- concepts.md 2.8 and plan 4.11: the launcher's resting content is the menu of section 3.5, not the New Tab page; the "Start something" and "Start from a template" content is the Getting Started app's.
- concepts.md 2.6 and contracts section 2: `text_param` on a launch path; the built-in manifests table gains Getting Started and the chat's `send`.
- contracts 5.1: `GET /api/templates-catalog` removed; 5.5: the inventory field; 7: `shell:start-with-text`; 11: the menu width token; 12: the selectors of 6.2.
- plan 15: "a richer launcher" stays deferred, the New Tab sections are no longer the launcher's.
- The window-bound-resources spec's section 9: "hiding the chat's `new` tile from the launcher" is resolved here (it is the primary text action); its decision 1 and concepts.md decision 15: Getting Started's shortcut is the second focus-mode shortcut beside the browser's, for the same reason (one window is what it is for).
- `system/apps/system_interface/README.md`: the launcher section and the routes list; `system/apps/README.md`: Getting Started among the built-in apps; the `manage-desktop` skill: nothing (no new op).
- `catalog/README.md`: the catalog is fetched by the Getting Started app.

## 13. Deferred and out of scope

- Searching inside apps from the launcher (an app-declared search route), as the desktop plan already defers.
- Ordering apps by recent use; the shell has no activity data.
- ~~The idempotency key for launch paths on linked windows (issue #646)~~: resolved by the post-launch-paths plan, which made every launch path that creates something a POST.
- A first-visit window on later desktops, for later clients, or a per-user first visit; a manifest-declared first window the shell would seed itself.
- Hover-revealed row actions (add to desktop, open in a new window); "Add to desktop" leaves with the tiles and returns when a row menu is designed.
- The chat shortcut opening a second chat list while a pinned one exists.

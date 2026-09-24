# Launch paths as POSTs, and the chat's intake route

This is the spec for taking the side effects out of the desktop's launch paths.
Today a launch path is a GET with a side effect: loading the chat's `/new?message=` creates a chat and sends the message, `/send?message=` sends a message, `/?draft=` fills a composer, the terminal's `/new` allocates a tmux session, and the browser's `/new` starts a browser.
The desktop model makes that visible, since a window is shared truth and every client's page follows a window's path, so the shell grew a `settling` state, deferred restores, and a per-client navigation guard to make each launch run once.
After this change a launch path may be a POST: the shell posts the launch's parameters to the app, the app does the work and answers with the pure path of the page that shows the result, and the shell opens or navigates a window there.
No page path has a side effect any more, and everything that existed to contain one goes.

The chat app also gains one route through which every text entering a chat from outside a chat page arrives: the intake route, which decides which chat receives the text (a new one, the one on screen, one the user picks) and whether the text is sent or drafted.

It is written for the people and agents implementing it, and it is the reference the implementation is judged against.
It builds on the desktop interface V1 ([plan](../desktop-interface/plan-desktop-interface.md), [concepts](../desktop-interface/concepts.md), [contracts](../desktop-interface/contracts.md)), the [pinned-taskbar-entries plan](../pinned-taskbar-entries/plan-pinned-taskbar-entries.md), the [launcher-and-getting-started plan](../launcher-and-getting-started/plan-launcher-and-getting-started.md), and the [chat-agent-split plan](../chat-agent-split/plan-chat-agent-split.md), and amends those documents where section 13 says.
It resolves issue imbue-ai/default-workspace-template#646 by taking its second option.

## 1. Purpose and principles

Three things are settled by this design:

1. **A page path is pure.** Loading, reloading, or following a window's path never creates, sends, or changes anything.
   A launch path that starts something is a POST, and the URL a window is opened or navigated at is the answer to that POST.
2. **The shell makes the POST, and names no app.** The shell backend posts to the app's registered URL what the manifest declares and what the caller supplied, and reads back a path.
   It knows nothing about chats, sessions, or browsers; the manifest says which launch paths are POSTs and what they take.
3. **Text enters the chat app through one route.** The launcher's rows, the Getting Started tiles, the avatar dialog's draft, and an agent's `layout.py open chat --launch ...` all post the same request to the chat's intake route, differing only in how the receiving chat is chosen and whether the text is sent or drafted.

The principles of the workspace app model hold: one owner per fact; the shell is generic; truth is shared while arrangement is scoped; minimal shell state; no two-phase commits.

## 2. Glossary

| Term | Meaning |
|---|---|
| Launch path | A way of starting something an app declares in its manifest (concepts.md 2.6); now either a GET launch path (a pure page path with optional query params) or a POST launch path (a route the shell posts to, which answers a page path) |
| Preset | A fixed name-value pair a launch path's manifest entry declares; the shell sends it with every launch of that path |
| Envelope | The fields the shell adds to every POST launch beside the params: the requesting client, its desktop, and the path of the window being launched into |
| Launch target | Where the shell puts the page the launch answers: a new window, an existing window at that path (focus), or a named window this client points at it |
| Intake | The chat app's one request for text arriving from outside a chat page: the text, how the receiving chat is chosen, and whether it is sent or drafted |
| Intake target | How the receiving chat is chosen: `new_chat`, `current_chat`, `chat_selector`, or `chat` (an explicit id) |
| Pending intake | An intake the chat app could not complete on the server (a draft, or a choice the user has to make), held under a one-time token until the chat root applies it |
| Provisional chat | A chat the chat app has minted whose first agent does not exist yet (`ProvisionalChat` today); the `awaiting_first_send` phase now covers unseeded chats too |

## 3. The model

### 3.1 GET and POST launch paths

A launch path gains three optional manifest fields:

```toml
[[launch_paths]]
id = "new"
label = "New Chat"
path = "/api/chats/intake"
method = "POST"
params = [
    {name = "account_id", label = "Provider account", required = false},
    {name = "message", label = "First message", required = false},
]
presets = {target = "new_chat"}
text_param = "message"
```

- `method` is `GET` (the default) or `POST`.
  A GET launch path is what every launch path was: the shell opens a window at `path` with the params as its query string, and the page at that path must be pure.
  A POST launch path is a route on the app's origin: the shell posts to it and opens a window at the path it answers.
- `presets` is an inline table of string values the shell sends with every launch of the path, beside the caller's params.
  A preset's name may not also be a declared param's name, and neither may be one of the envelope's reserved names (section 3.2).
  A GET launch path's presets go into the query string like params.
- `draft_param` names one declared param, as `text_param` does, and marks the launch path as one that takes text to be drafted rather than sent.
  A launch path declares at most one of `text_param` and `draft_param`.
  The launcher lists either kind as a free-text row (section 4.1); the avatar dialog's "Design your own..." looks for a `draft_param` (section 4.3).

The registry row carries `method`, `presets`, and `draft_param` beside the fields it carries today, each with its default when the manifest omits it, and the inventory's `app` object and `apps_updated` carry them on each launch path.
A registry row written by an older release lacks all three and reads as a GET launch path with no presets and no draft param, which is exactly what it was.

The synthesized `open` launch path of an app that declares none stays a GET at `/`.

### 3.2 The launch: what the shell posts, and what it reads back

A POST launch is one request from the shell backend to `<app url><path>`, over loopback, with a JSON object body: the presets, then the caller's params (a param the caller supplies for a name the launch path does not declare is refused before anything is posted), then the envelope:

| Field | Value |
|---|---|
| `client_id` | The requesting client's id; absent for an op with no client |
| `desktop_id` | The desktop the launch is for; absent for an op with no client |
| `window_path` | The path of the window the launch is aimed at, as the requesting client sees it (section 3.3's `window` target); absent for the other targets |

These three names are reserved: a manifest that declares a param or preset by one of them fails to load.
Every value is a string; an app that wants a boolean declares a preset such as `is_draft = "true"` and reads it as one.

The app answers `200` with `{"path": "<a path under its origin>"}`.
The shell holds the path to the window-path rule (contracts.md section 1) and refuses anything else.
An app that answers `4xx` with `{"detail"}` has refused the launch, and the shell answers its caller `400` with `<app> refused the launch: <detail>`; an app that cannot be reached, times out (30 seconds), or answers anything else is a `502`.

A GET launch is the same resolution with no request: the path is `path` plus the presets and params as a query string, held to the window-path rule (a text over the 2048-character bound is a `400`).

### 3.3 Launch targets

The shell's one launch route (section 5.3) takes a target saying where the answered path goes:

- `new`: open a window of the app at the path on the desktop, placed for the requesting client (the open of contracts.md section 5.3 with `if_present = new`).
- `focus`: a window of the app already at exactly that path is focused instead; otherwise as `new`.
- `window`: point a named window of the app at the path, as the op route's `navigate` does (an independent window moves for this client alone; a linked window moves for everyone), and answer that window.
  The window's current path as this client sees it rides in the envelope as `window_path`.

With a POST launch path the `window` target is what the launcher's free-text rows use (section 4.1), and it is safe on a linked window too: the path every client follows is pure, so nothing runs twice.

### 3.4 Windows without settling

A window is `{id, app, path, title, opened_at, is_pinned, scope}`.
`is_settling` is gone: no window is ever opened at a path that does something, so no client has to hold back from showing one.
Every client creates the page for a window it shows, whoever opened it; a restore is never deferred; a visiting user's seeded desktop copies every window of the first desktop.
`WindowOpenRequest` loses `launch`, which existed only to set the flag.

A `desktops.json` written by the previous release carries `is_settling` on each window; the store strips it on read, beside the retired desktop keys it already strips, with a `CLEANUP:` note.

### 3.5 The chat's intake

`POST /api/chats/intake` takes one JSON object (`IntakeRequest`):

| Field | Type | Meaning |
|---|---|---|
| `message` | string | The text; may be empty for a `new_chat` that starts with nothing |
| `target` | `new_chat` \| `current_chat` \| `chat_selector` \| `chat` | How the receiving chat is chosen |
| `chat_id` | string, default `""` | The chat, when `target` is `chat` |
| `is_draft` | bool, default false | Put the text in the chat's composer, unsent, instead of sending it |
| `account_id` | string, default `""` | The account a new chat starts on; empty picks the default, as a create does |
| `is_delivery_awaited` | bool, default false | Answer only once the send has been accepted by the harness (or refused), instead of as soon as the receiving chat is decided |
| `client_id`, `desktop_id` | string, default `""` | The client the text came from, for the shell's client-activity attribution; both or neither |
| `window_path` | string, default `""` | The path of the window the text was typed into, which `current_chat` reads |

The receiving chat resolves in this order:

- `chat`: the chat `chat_id` names among the chats the app lists (an agent, a recorded chat, or a provisional one); `404` otherwise.
- `current_chat`: the chat `window_path` selects (`/?chat=<id>`, `/<id>`, or a subagent view `/<id>.<agent>.<session>` of it) when the app lists it; else the chat most recently messaged; else the rules of `new_chat`.
- `chat_selector`: the chats that are agents (a provisional chat cannot take a message).
  With none, the rules of `new_chat`; with one, that chat; with more, the choice is the user's, and the intake is held (section 3.6).
- `new_chat`: the account is `account_id`, else the workspace's default (the pinned default, else the most recently used).
  With an account and `is_draft` false, the chat is created through the existing create path with `message` as its first message, exactly as the chat root's New Chat button creates one.
  Otherwise (no account, or a draft) the app mints an unseeded provisional chat in the `awaiting_first_send` phase (section 3.7) and holds the intake for it.

Once the chat is decided:

- A send to a chat that is an agent goes through the ordinary send path (`_deliver_message`: held during a handoff, revived when stopped, attributed to the client when `client_id` and `desktop_id` are given).
  With `is_delivery_awaited` false it runs on a background thread and a failure is logged at warning level; with it true the route waits and answers the send route's own failure codes (`500` with the harness's detail, `503` not ready).
- A draft, a send to a provisional chat, or a choice is held as a pending intake (section 3.6).

The route answers `200` with `{"path"}`: `/?chat=<id>` for a completed send or create, `/?chat=<id>&intake=<token>` for a held draft or a held first message, and `/?intake=<token>` for a choice.
Like every create, it answers `503` until the agent list has been read from mngr once.

### 3.6 Pending intakes

A pending intake is the intake request plus the chat it resolved to (or none, for a choice), held in memory under a one-time token (`secrets.token_urlsafe`) for fifteen minutes.
It is not persisted: a chat-app restart drops it, and the chat root then finds nothing to apply, which is the same as a token already applied.

Three routes, all for the chat root:

| Route | Answer |
|---|---|
| `GET /api/chats/intakes/<token>` | `{"message", "is_draft", "needs_pick", "chat_id"}`: the text, whether it is a draft, whether the root has to offer the picker, and the chat it resolved to (`null` for a choice); `404` once applied, dismissed, expired, or never minted |
| `POST /api/chats/intakes/<token>/apply` with `{"chat_id"?}` | Consumes the token and finishes the intake: `{"path", "chat_id", "composer_text", "first_message"}` (section 3.6.1); `400` when a choice names no chat or a chat that is not an agent; `404` as above |
| `DELETE /api/chats/intakes/<token>` | Drops the intake unapplied (the picker was dismissed); `204`, also for an unknown token |

#### 3.6.1 What apply answers

`chat_id` is the receiving chat, and `path` is `/?chat=<chat_id>`.
Exactly one of these holds:

- `composer_text` is the text and `first_message` is null: a draft. The root puts the text into that chat's composer, unsent.
- `first_message` is the text and `composer_text` is null: a send to a provisional chat awaiting its first send, with no account signed in when the intake arrived. The root launches the chat with it (section 4.6): the provider chooser opens, and the sign-in launches the chat's first agent with the text as its first message.
- Both null: a send to a chat that is an agent, delivered by the server on apply through the ordinary send path (on a background thread), so a picked chat is messaged the way one named in the intake is.

The token is consumed before the answer, so a second apply is a `404`.

### 3.7 Unseeded provisional chats

`ProvisionalChat`'s `awaiting_first_send` phase, which a seeded chat has used since the welcome chat landed, now covers a chat with no seed: one minted by an intake that could not launch at once.
Such a chat has a display name minted like a create's ("Chat N"), the account the intake resolved (or none), no message, and `is_seeded` false.
It is listed among the chats as a provisional chat is, its page shows an empty conversation with the composer under it, and the user's first send launches it, on the signed-in account (the provisional chat's own when it names one, else the selected one) or through the provider chooser when nothing is signed in, exactly as a seeded chat's first send does.
A launch by `chat_id` of an unseeded awaiting chat brings the message, as a seeded one's does.
Discarding it before its first send drops it; it has no record, so a chat-app restart drops it too, which is accepted (its draft was the page's alone).

### 3.8 Ownership

| Fact or verb | Owner |
|---|---|
| Whether a launch path is a GET or a POST, its presets, and which param takes drafted text | The app's manifest, through the registry |
| Making the POST, validating the params, reading the path, opening or navigating the window | The shell backend, in its launch route and the op route's `open` |
| Which chat receives a text, whether it is sent or drafted, and when a choice is the user's | The chat app, in its intake route |
| Holding a draft or a choice until the page can act on it | The chat app, in its pending intakes |
| Putting a draft into a composer, offering the picker, opening the chooser for a first message | The chat root, on applying a pending intake |
| Creating a tmux session or a browser and naming its page | The terminal and browser apps, in their `POST /new` |

### 3.9 Invariants

- No path a window can be at has a side effect when loaded, reloaded, or followed.
- The shell never builds a URL that carries text to be sent, and never reads a body it posts beyond the reserved envelope names.
- One press of a launcher row, tile, or shortcut, and one op, makes at most one chat, session, or browser, however many clients show the window.
- A pending intake is applied at most once.
- `is_settling` appears nowhere: not on the wire, not in a state file this release writes, not in a page.

## 4. Behaviour

### 4.1 The launcher

The free-text rows are every launch path with a `text_param` or a `draft_param`, in launcher order, the first the primary action (Enter) and the second the secondary (Ctrl+Enter), as the launcher plan's section 3.1 has them.
On a stock machine that is the chat's `new` (primary), `send` (secondary), and `draft` (a row with no key binding).
Running one with text `t` fills its `text_param` or `draft_param` with `t` and launches, pinned-first: when the app has a pinned window on the active desktop (whatever its scope), the launch's target is that window, and the window is restored and raised; otherwise the target is `new`.
A launch-path row runs as before: a GET launch path at its app's pin path raises the pinned window; every other launch path launches with target `new`.
The 2048-character bound applies to GET launch paths alone; a POST launch path's row is never disabled for length.

### 4.2 Getting Started

A tile posts `shell:start-with-text`; the shell runs the primary free-text row with the text, as section 4.1 says.
On a stock machine the chat's `new` is posted with the text as `message`, the chat is created, and the pinned chat window is pointed at `/?chat=<id>`.

### 4.3 The avatar dialog's draft

"Design your own..." looks for a pinned app on the active desktop declaring a launch path with a `draft_param`, and is disabled with its tooltip when none does.
It launches that path with the prompt as the draft param and the pinned window as the target.
On a stock machine the chat's `draft` posts `target = current_chat`, `is_draft = "true"`, the prompt, and the pinned window's path; the chat resolves the chat on screen (else the most recent, else a new provisional one), holds the draft, and answers `/?chat=<id>&intake=<token>`; the pinned window moves there for this client, and the root applies the intake into that chat's composer and reports `/?chat=<id>`.

### 4.4 An agent's open

`layout.py open chat --launch new --param message="..."`, `open terminal --launch new --param workdir=/data`, and `open https://example.com` keep their spelling.
The op route's `open` resolves the launch path through the same code as the launch route: a POST launch path is posted (with `client_id` and `desktop_id` when the op has a client, and no `window_path`), and the window opens at the answered path with `if_present` deciding focus or new; a GET launch path opens at its path with the query string as today.
`layout.py list` and `desktops` no longer print `is_settling`.

### 4.5 Deep links and shortcuts

The shell root's `?launch=<app>:<id>` runs the launch path with target `new` through the launch route once the apps are known, and the param is stripped from the URL as today.
A shortcut in `new` mode launches with target `new`; in `focus` mode it raises the app's most recently focused window first, as today, and launches only when there is none.

### 4.6 No account signed in

A `new_chat` intake with nothing signed in mints an unseeded provisional chat and holds the intake.
The window lands on `/?chat=<id>&intake=<token>`; the root applies the intake and receives `first_message`, so it opens the provider chooser over the chat; signing in launches the chat's first agent with the text as its first message, and dismissing the chooser puts the text into the composer instead, where the next send offers the chooser again.
A `chat_selector` or `current_chat` intake never asks for an account: it reaches an existing chat, which has one, or falls into `new_chat`.

### 4.7 Reloads and other clients

A window at `/?chat=<id>&intake=<token>` reloaded before the root applied the intake applies it then; reloaded after, the root asks about a consumed token, is told `404`, and shows the chat.
The root reports `/?chat=<id>` as soon as it has applied or given up, so the stored path carries no token for long, and a second client following a linked window to a token path applies nothing when the first has.
A picker left open on one client and applied on another applies once; the other client's apply is a `404`, and it dismisses its picker.

### 4.8 Terminal and browser

`POST /new` on the terminal with `{"workdir"?}` allocates the session and answers `{"path": "/?session=<name>"}`; on the browser with `{"url"?}` starts the browser and answers `{"path": "/?session=<name>"}`.
The GET handlers go; `GET /new` is a `405`.
Both read only the params they declare and ignore the envelope.

## 5. The shell backend

### 5.1 `app_manifest`

- `LaunchPathMethod` (`GET`, `POST`), `LaunchPath.method` (default `GET`), `LaunchPath.presets: dict[LaunchParamName, str]` (default empty), `LaunchPath.draft_param: LaunchParamName | None` (default None, validated against `params` like `text_param`; at most one of the two).
- `RESERVED_LAUNCH_PARAM_NAMES = {client_id, desktop_id, window_path}`: a param or preset by one of these names, or a preset by a declared param's name, fails validation.
- `RegistryLaunchPath` gains the same three fields with the same defaults.
- `forward_port.py` copies `method` (when given), `presets` (when non-empty, as a table of strings), and `draft_param` (when given) onto the row's launch path entry.

### 5.2 State and wire

- `Window` loses `is_settling`; `desktops.py` strips the key on read (a `_RETIRED_WINDOW_KEYS` beside `_RETIRED_DESKTOP_KEYS`, with a `CLEANUP:` note dated like it).
- `WindowOpenRequest` loses `launch`; `_append_window` takes no launch.
- `settled_windows` goes; `desktop_seeded_from` copies every window.
- `launch_path_wire_json` carries `method`, `presets`, and `draft_param`.

### 5.3 The launch route

`POST /api/desktops/<desktop_id>/launch` with `LaunchRequest`:

```json
{"app": "chat", "launch": "new", "params": {"message": "hi"}, "client_id": "...",
 "target": {"kind": "window", "window_id": "win-..."}, "minimized": false}
```

- `target.kind` is `new`, `focus`, or `window`; `window_id` is required for `window` and must name a window of `app` on the desktop.
- The shell resolves the launch path (`400` for an unknown app, launch id, or param; the registry row carries the param names alone, so a missing required param is the app's to refuse, which a POST launch path does with a `4xx` and its `detail`), builds the path (a GET) or posts for it (a POST, section 3.2), then opens (`new`, `focus`; `minimized` as the op's) or navigates (`window`) and answers `{"window", "path", "is_new"}` with `201` for an open and `200` otherwise.
- The POST is made by `shell/launches.py`, behind a `launch_poster` field on `ShellState` injected like `close_hint_poster`, so the routes are tested against a recording poster and the e2e suite against a stub app.

The op route's `open` uses the same resolution (`_open_target` returns the answered path for a POST launch path); `WindowOpenRequest` no longer carries a launch.

## 6. The shell frontend

- `model/records.ts`: `LaunchPath.method`, `presets`, `draft_param`; `WindowRecord` loses `is_settling` (a stray one on the wire is ignored).
- `model/api.ts`: `launch(desktopId, request)` posting the launch route; `openWindow` drops `launch`.
- `model/launch.ts`: `freeTextRowsOf` and `launchRowKindOf` treat a `draft_param` like a `text_param`; `fillParamOf(launchPath)` names the param a free-text row fills; `textPathOf` becomes `textRowDisabledReason`, which bounds GET launch paths only; `launchPathWithParams` goes (the backend builds every path).
- `store/DesktopStore.ts`: `launchAt(app, launchId, params, target)` calls the route and applies the answer (a `new` or `focus` answer as `openWindowAt` applies an open; a `window` answer as `navigateOwnWindow` applies a location, marking the own navigation so the page follows).
  `runLaunch`, `runLaunchRow`, `runFreeText`, `startWithText`, `draftIntoPinnedWindow`, and `applyDeepLink` go through it.
  `pendingRestores`, `deferWhileSettling`, and `isPlacedHere` go; `raiseWindow`, `restoreWindow`, `setWindowState`, and `toggleMaximized` act at once.
- `reducers/desktopState.ts`: `draftTargetOf` finds the pinned app's launch path with a `draft_param`.
- `pages/livePages.ts`: every shown window gets a page.
- `views/Window.ts` and `views/TaskbarEntry.ts`: the "Starting on another screen" placeholder and the settling dimming go.
- `testing/fakeShell.ts`: `launch`, and no `is_settling`.

## 7. The chat app

### 7.1 Backend

- `models.py`: `IntakeTarget`, `IntakeRequest`, `IntakeResponse`, `PendingIntakeView`, `IntakeApplyRequest`, `IntakeApplyResponse`.
- `chat_intakes.py` (new): the resolution of section 3.5 as pure functions over the manager's chat snapshots and provisional chats (`chat_selected_by_path`, `most_recently_messaged_chat`, `resolve_intake_target`), and `PendingIntakeStore` (mint, get, take, discard, expiry, under a lock).
- `agent_manager.py`: `mint_awaiting_chat(account_id) -> ProvisionalChat` (an unseeded `awaiting_first_send` chat with a minted name, broadcast like a seeded one); `create_chat` with a `chat_id` accepts an unseeded awaiting chat, whose launch brings the message.
- `server.py`: `POST /api/chats/intake`, the three `/api/chats/intakes/<token>` routes; `/new` and `/send` are no longer served (the root is served at `/` alone).
- `app.toml`: `root` stays a GET at `/` with no params (the chat list; the launcher's focus row and the desktop shortcut); `new`, `send`, and `draft` are POST launch paths at `/api/chats/intake` with the presets of section 3.5 (`target = new_chat`; `target = chat_selector`; `target = current_chat` and `is_draft = "true"`), `new` and `send` with `text_param = "message"` and `draft` with `draft_param = "message"`.

### 7.2 Frontend

- `root/selection.ts`: `intakeTokenFromSearch`; the `/new`, `/send`, `message`, `account_id`, and `draft` readers go.
- `root/index.ts`: on load and on `shell:navigate`, a token is fetched and applied (section 3.6): the picker over the agent chats for a choice (Escape deletes the token), else apply at once; the answer selects the chat, drafts `composer_text`, or launches with `first_message` through the chooser (dismissal drafts it); then the root reports the selection alone.
  `startNewChat` stays for the New chat button.
- `root/SendPicker.ts`: unchanged in shape; its `onPick` applies the token.
- `models/Chats.ts`: `fetchPendingIntake`, `applyPendingIntake`, `discardPendingIntake`.
- `views/ChatPanel.ts`: an unseeded `awaiting_first_send` chat renders an empty conversation with the composer.
- `views/MessageInput.ts`: the first send of an awaiting chat prefers the provisional chat's own account when it names one.

## 8. Terminal and browser

- `terminal/pages.py`: `@blueprint.post(NEW_PATH)` reading `workdir` from the JSON body (absent or empty for the default), answering `{"path"}`; the redirect and the GET go.
- `browser/runner.py`: `new_browser` on `POST`, reading `url` from the JSON body, answering `{"path"}`; `_start_browser`'s error responses pass through.
- `terminal/app.toml` and `browser/app.toml`: `method = "POST"` on `new`.

## 9. `layout.py` and the skills

- `_listed_window` drops `is_settling`; the module docstring's `open` paragraph says a launch path may be a route the app is asked for its page.
- `manage-desktop/SKILL.md`: the `is_settling` line goes; the launch-path glossary row names both kinds.

## 10. Errors and edge cases

- A launch whose app is stopped: the POST fails to connect and the shell answers `502 <app> could not be reached`; the user is told through the store's notification.
- An intake for `current_chat` whose `window_path` names a chat that has since been destroyed falls through to the most recently messaged chat.
- A `chat_selector` with several chats and `is_draft` true: the picker shows the text and picking drafts it into the chosen chat.
- An empty `message` with `target = new_chat`: the create runs with no message, as the New chat button's does.
- An empty `message` with any other target: the intake answers the path of the resolved chat and sends nothing.
- A `new_chat` intake naming an `account_id` the workspace does not have: `400` with the account error, passed through by the shell.
- Two clients following one linked window at a token path: one apply consumes the token; the other's `404` shows the chat with nothing drafted, which is accepted.
- A pending intake whose chat is destroyed before apply: apply answers `404`.

## 11. Testing

- `app_manifest`: `method`, `presets`, and `draft_param` parse and validate (a reserved name, a preset shadowing a param, both text and draft params); the registry copies them; `forward_port` writes them.
- Shell backend: the launch route against a recording poster (a GET launch builds the path; a POST launch posts presets, params, and the envelope, opens at the answer, refuses a bad answer with `502` and a refusal with `400`, and navigates a named window); the op route's `open` with a POST launch path; `desktops.json` with `is_settling` reads.
- Shell frontend: the store's `launchAt` against the fake shell for each target; the free-text rows including a draft row; `draftTargetOf`; no settling anywhere.
- Shell e2e: the stub app serves `POST` launch routes; the launcher's rows point the pinned window at the answered path; the deep link runs a POST launch; the settling scenarios go.
- Chat backend: the intake resolution per target; the pending store's lifecycle; the routes; an unseeded awaiting chat's launch.
- Chat e2e: a draft token into the shown chat; a choice with two chats (Escape drops, a pick sends and selects); a `new_chat` with no account offers the chooser after the window lands; the send route through the intake with one chat.
- Terminal and browser: `POST /new` answers a session path; `GET /new` is `405`.
- `system/test_app_manifests.py`: the contract table gains the new fields.

## 12. Implementation order

One pull request per repository; every step leaves the tree green.

1. `app_manifest`, `forward_port.py`, the registry, and the inventory wire: the three fields.
2. The shell backend: `launches.py`, the launch route, the op route's `open` through it.
3. The chat app: the intake and pending-intake routes, the unseeded awaiting chat, the manifest.
4. The terminal and browser `POST /new`.
5. The shell frontend: `launchAt` and its callers; the chat root: the token flow.
6. Settling removed: backend, wire, frontend, `layout.py`, the skill.
7. Documents (section 13) and changelog entries for `dev`, `app_manifest`, `system_interface`, `chat`, `terminal`, `browser`, and `agents`; the mngr-side pull request (`apps/minds/docs/design.md` and a changelog entry; the Electron e2e runner presses launcher rows and waits for a chat frame, so it is expected to pass unchanged and is run to confirm).
8. Manual verification in a dev workspace: Enter, Ctrl+Enter, a Getting Started tile, "Design your own...", a second client showing the same pinned window, `layout.py open terminal`, and a reload of a window mid-launch.

## 13. Amendments to existing documents

- concepts.md 2.6: a launch path is a GET page path or a POST route the shell asks for a page path.
- contracts.md section 2: the manifest table gains `method`, `presets`, and `draft_param`, and the reserved names; section 3.3 and 4.1 of the plan and contracts 4.1 lose `is_settling`; contracts 5.3 gains the launch route and its `windows` route loses `launch`; contracts 8's `open` says a POST launch path is posted for its path; contracts 9's `launch=` runs through the launch route.
- The launcher plan's section 3.2 note (the GET with a side effect, issue #646), section 8 (the chat's `send` at `/send`), and section 13 (the idempotency key) are superseded by this document.
- The pinned-taskbar-entries plan's section 4.7: the draft goes through the `draft_param` launch path.
- The window-bound-resources spec's decision 5: a window at a launch path no longer exists; a resource is collected once a window showed it and none does.
- `system/apps/chat/README.md`, `system/apps/system_interface/README.md`, `system/apps/terminal/README.md`, `system/apps/browser/README.md`, and the `manage-desktop` skill.

## 14. Deferred and out of scope

- Moving `system/scripts/message_chat.py` onto the intake route (it keeps the send and create routes, which stay).
- Persisting pending intakes or unseeded awaiting chats across a chat-app restart.
- Surfacing a background send's failure in the chat page (it is logged).
- The desktop shortcut opening a second chat list while a pinned one exists (window-bound-resources decision 2).

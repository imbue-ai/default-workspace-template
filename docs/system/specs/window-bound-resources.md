# Window-bound resources and new-window shortcuts

Status: agreed design (2026-09-20), implemented on `mngr/desktop-ui-phase-6`.
Audience: implementers of `system/apps/terminal`, `system/apps/browser`, `system/apps/chat`, `system/libs/app_manifest`, the shell (`system/apps/system_interface`), `system/scripts/layout.py`, and the `manage-desktop` and `agentic-browser-fleet` skills.

This spec amends the desktop interface ([plan](../blueprint/desktop-interface/plan-desktop-interface.md), [contracts](../blueprint/desktop-interface/contracts.md), [concepts](../blueprint/desktop-interface/concepts.md)) in two places that the first live test showed do not feel like a desktop:

- **Part A: shortcuts.** Every desktop shortcut opens a new window of its app, and the app decides what a new window holds.
  The chat's new window is its chat list; a new chat is made only inside the chat app or by the workspace's own seeding.
- **Part B: window-bound resources.** A terminal session or a browser lives exactly as long as some window shows it.
  Each app collects its own resources by watching the shell's windows, prompted by the shell when a window closes; the shell stays generic and destroys nothing.
- **Part C: agents.** An agent that wants a terminal or a browser opens a window for it, so the lifetime rule holds for agents too.

## 1. Background

Facts this design builds on, as of `mngr/desktop-ui-phase-6` with the cleanup fixes merged (every app serves `app_contract.js` from its own origin, so terminal and browser windows report their real paths).

- A **window** is `{app, path, title}` on one desktop, shared by every client ([plan 3.3](../blueprint/desktop-interface/plan-desktop-interface.md)).
  A close removes the window for everyone and does nothing else: "the shell has no notion of stopping or deleting what the window showed" (plan 4.5; concepts decision 9 took "close means stop" from neither prototype).
- A **shortcut** runs a launch path in `focus` or `new` mode; the desktop is seeded from each manifest's `default_shortcut`.
  Chat seeds `{new, mode = new}`; terminal, files, and browser seed `{new, mode = focus}`.
  `ShortcutIcon.ts` labels a `new` shortcut with the launch path's label (then "New Terminal") and a `focus` shortcut with the app's display name ("Terminal"), so today's desktop reads "New Chat, Terminal, File Viewer, Browser", and flipping a mode from the context menu renames the icon.
- The **terminal** (`system/apps/terminal`) allocates `terminal-<N>` at `/new`, remembers it in `data/.apps/terminal/instances.json`, and recreates remembered sessions at startup.
  `TmuxSessionSource.delete_terminal` kills the session and forgets the record, but no route calls it.
  Every window of the terminal shows `/?session=<name>`.
- The **browser** (`system/apps/browser`) runs one headful Chromium per browser, each on its own `--user-data-dir` profile and its own Xvfb display, filmed per viewer by pixelflux; a browser accepts up to eight viewers.
  `BROWSER_MAX_SESSIONS` caps the fleet at 2.
  `stop_browser` ends Chromium and its display while keeping the profile and the tab list; `close_and_forget` (`DELETE /browsers/<name>`, the fleet CLI's `close`) also deletes the profile.
  `/new[?url=]` creates a browser and redirects to `/?session=<name>`.
  Chromium refuses two processes on one profile directory, so concurrent browsers cannot share a profile.
- The **fleet CLI** (`agentic-browser-fleet new`) creates a browser and already opens a viewer window through `layout.py open browser --path /?session=<name>`, falling back to a printed hint when the shell cannot place it.
  The **lease** (`acquire`, `release`, `handoff`) says who is driving; it is not a lifetime.
- The **op route**'s `open` writes the requesting client's placement on top, shown, and refuses with 412 when no client can be resolved (contracts section 8).
- In the staging workspace before the cleanup fixes, every terminal window sat at `/new` (the wrapper could not load the contract module) and four tmux sessions backed two windows: reloads of a settling window re-ran the launch path.
  The contract fix ends the accumulation; this spec ends the leak.

## 2. Decisions

Recorded here so the implementation need not re-argue them.

1. Every seeded shortcut is in `new` mode: a shortcut is "a new window of this app".
   A shortcut is labelled with its app's display name whatever its mode; launch path labels appear only on the launcher's rows.
2. A new chat window is the chat list at `/`.
   The chat app creates chats only from its own page (the New chat button, the launcher's free-text row and the Getting Started app's seeded prompts through `/new`) and the workspace's seeding; the desktop shortcut and `layout.py open chat` never create one.
3. A terminal and a browser are **window-bound**: the app destroys the resource once no window on any desktop shows it.
   The files app and the chat have no window-bound resource.
4. Collection is the **app's**, by a sweep over the shell's windows: run when the shell says a window of the app closed, and every 90 seconds as the safety net.
   The shell's close hint is a manifest-declared path the shell posts to; it carries no obligation, and a missed post costs at most one interval.
5. A resource is collected only after the app has **seen a window for it** and then sees none.
   A resource that never had a window (an agent's browser with nobody connected, a hand-made tmux session, a window still settling at `/new`) is never collected.
6. The fleet holds **one browser**.
   Closing its last window **stops** it (profile and tabs kept); `/new` brings the same browser back.
   Its profile is never deleted by the desktop; only the fleet CLI's explicit `close` deletes it.
7. The browser shortcut is the one `focus`-mode shortcut: "show me the browser".
8. An agent that wants a terminal or a browser opens a window for it, minimized, on the desktop of the client it serves; with no client connected the window is still opened, unplaced.
   The lease stays purely "who is driving".

## 3. Part A: shortcuts

### 3.1 Manifest defaults

| App | `default_shortcut` | `launch_paths` | Desktop label |
|---|---|---|---|
| `chat` | `{launch = "root", mode = "new"}` | `root` ("Chat", `/`), then `new` ("New Chat", `/new`, params `account_id`, `message`) | Chat |
| `terminal` | `{launch = "new", mode = "new"}` | `new` ("Terminal", `/new`, param `workdir`) | Terminal |
| `files` | `{launch = "new", mode = "new"}` | `new` ("File Viewer", `/`, param `path`) | File Viewer |
| `browser` | `{launch = "new", mode = "focus"}` | `new` ("Browser", `/new`, param `url`) | Browser |

The chat gains a second launch path, `root`, listed first so the launcher's rows show "Chat" before "New Chat".
The `new` launch path stays: the launcher's primary free-text row and the Getting Started app's intents and templates seed a chat through the launch path that declares `message` as its `text_param`, the welcome chat's auto-open targets `/?chat=<id>` explicitly, and `layout.py open chat --launch new` remains the way an agent starts a chat.
The browser's launch path was relabelled "Open Browser" because it no longer always creates (section 5.4); the launcher-and-getting-started plan later shortened the three single-launch-path apps' labels to their display names ("Terminal", "File Viewer", "Browser"), since the launcher's row is the one place they show.

`shortcutLabel` in `ShortcutIcon.ts` returns the app's display name in both modes, so a shortcut's icon never renames when its mode changes; the launch path's label is what the launcher's row shows, beside the app's display name.

### 3.2 What a run does

`resolveLaunchRun` is unchanged.
A `new` shortcut opens a window at the launch path with `if_present: new`, so every double click on Chat opens another list window and every double click on Terminal allocates another session.
The `focus` browser shortcut raises this client's most recent browser window on this desktop and opens `/new` only when there is none.

`layout.py open <app>` with no `--path` or `--launch` follows `default_shortcut.launch`, so an agent asked to "show the chat" opens the list.

### 3.3 Other defaults

- `system/test_app_manifests.py` pins the modes and launch paths of section 3.1.
- The e2e stub app keeps its `focus` seed; the focus tests stay as the coverage of that mode.

### 3.4 Existing desktops

Shortcuts are seeded once, when a desktop is created, so an existing desktop keeps its stored shortcuts (the chat's at `(chat, new)` in `new` mode, the others in `focus` mode).
There is no automatic migration in this release, as for every other desktop-file change (plan section 15).
A user flips a shortcut from its context menu ("Change shortcut to ...") or makes a new desktop; an agent runs `layout.py shortcut remove chat --launch new` and `layout.py shortcut set chat --launch root --mode new --cell <column,row>`.
The changelog entry says so.

## 4. Part B: window-bound resources

### 4.1 The rule

An app that owns window-bound resources runs a **sweep** when the shell posts its close hint (section 4.6) and on a fixed interval (90 seconds) regardless.
Each sweep reads the shell's desktops and derives, for this app, the set of resource keys its windows name; then for every resource the app remembers:

- a window names it: mark the record **window-seen** (persisted, once);
- no window names it, and it is window-seen: **collect** it;
- no window names it, and it was never seen: leave it.

The close hint (section 4.6) names the closed window's path.
Before the sweep it brings, the app marks the resource that path names window-seen, so a window that opened and closed between two sweeps (no periodic sweep ever observed it) still counts as having shown its resource; without this a terminal or browser closed within its first sweep interval would never be collected.
A hint whose path names nothing (a window closed while still at `/new`) marks nothing.

A sweep that cannot read the desktops (the shell down or restarting, a non-JSON or wrongly shaped answer) does nothing and logs at debug.
"No windows" is only ever a fact the shell stated.
The first periodic sweep runs one interval after the app starts, so a shell still booting beside the app is not asked too early; a hint runs a sweep at once whenever it arrives.
Sweeps are serialised: a hint during a sweep queues one more sweep rather than running two.

Resources that exist before this release (the staging workspace's four terminal sessions with two windows) are never window-seen and are never collected; they are cleaned up by hand once, and the changelog entry says so.

The sweep looks at every desktop, `shared` or `personal` alike: a window anywhere keeps the resource.
A window at a path the app cannot parse (a settling `/new?workdir=...`, an unknown query) names no resource.

**Note:** a `desktops.json` that is reset or restored from an older backup drops every window at once, and every window-seen resource is then collected on the next sweep.
For terminals that is a lost shell; for the browser it is a stop, which keeps the profile (section 4.4).
This is accepted for V1.

### 4.2 Reading the shell's windows

A small stdlib reader in `system/libs/app_manifest` (the library both apps already depend on, beside `register_app`):

```python
def read_app_window_paths(shell_url: str, app: AppName) -> list[str] | None:
    """Every window path of ``app`` across every desktop, or None when the shell could not be read."""
```

It GETs `{shell_url}/api/desktops` with a 2 second timeout over `urllib.request`, validates the `{"desktops": [{"windows": [{"app", "path", "client_paths"?}, ...]}, ...]}` shape it needs, and returns the paths of the windows whose `app` matches: each window's `path`, and every value of its `client_paths` (a pinned window with the `independent` scope keeps its shared path at its home path, and what each client's page shows rides beside it; any one of those views keeps the resource alive).
The shell URL is `MINDS_WORKSPACE_SERVER_URL` with the default `http://127.0.0.1:8000`, resolved as `layout.py` and the chat's `shell_client.py` resolve it; a helper `shell_base_url()` moves into the same module so the three agree.

A window-seen flag is one additive boolean on each app's record, defaulting to false, so a store written by the previous release reads unchanged.

### 4.3 The terminal

- The resource key is the `session` query parameter of `/?session=<name>`, parsed with `urllib.parse`.
- `TerminalSessionRecord` gains `is_window_seen: bool = False`; the store version stays 1.
- `TmuxSessionSource` gains `sweep_windows(window_paths)`: for each remembered record, set the flag when a path names it (one `save_record` per newly seen terminal); collect a window-seen record no path names.
  Collecting a live terminal is `delete_terminal` (kill the session, forget the record, drop the id file); collecting a stopped record is forgetting it.
  A record whose name is an agent session is never collected (`delete_terminal` already refuses it).
- Hand-made sessions (listed, never recorded) are never collected: nothing records them.
- The sweep thread starts in `run_terminal_app` after the remembered sessions are recreated and stops on shutdown; `main.py` wires the shell URL and the interval.
- The pages blueprint gains `POST /api/window-closed`, the manifest's `window_closed_path`, which marks the terminal the posted path names window-seen, wakes the sweep thread, and answers 204.
- The README's "verbs no route offers yet" paragraph is rewritten: delete is what the sweep calls.

### 4.4 The browser

**One browser.** `BROWSER_MAX_SESSIONS` defaults to 1 (the memory of a second Chromium is what a small workspace cannot afford anyway).
The cap counts launched browsers (`init`, `running`); a stopped browser holds no slot.
Restore honours the cap: the first saved browser by name relaunches and every further saved browser is registered stopped, never refused.

**`/new` means "the browser".**
`GET /new[?url=]`:

- a browser exists and runs: redirect to its page; with `url`, first open the URL as a new tab in it (the daemon's CDP client already opens tabs for restore);
- a browser exists and is stopped or crashed: `start_browser` it (with `url` as an extra tab once it is up), then redirect;
- no browser exists: create as today, then redirect.

With several saved browsers (a workspace upgraded from a cap of 2), "the browser" is the running one, else the first by name.
`POST /browsers` with no `name` answers the same browser the same way, so the fleet CLI's `new` needs no change beyond section 6; a `POST /browsers` with a `name` keeps its create-or-409 semantics for a named second browser an operator insists on.

**Collection is a stop.**
The manager gains a sweep (on the bridge loop, `bridge.submit`) that applies section 4.1 with the `session` query parameter as the key, run on the interval and whenever `POST /api/window-closed` (the manifest's `window_closed_path`) arrives, which first marks the browser the posted path names window-seen.
Collecting a browser is `stop_browser`: Chromium, its audio sink, and its display go; the profile and the tab list stay; `stopped: true` is checkpointed.
Collecting a browser that is already stopped, or still launching, is a no-op (a launching one is collected on a later sweep once it runs and still has no window).
The viewer's "New browser" gate (`can_create` in `GET /browsers`) is true whenever Chromium is installed, since `/new` always has a browser to answer.
The window-seen flag lives on `LiveBrowser` and rides `ManifestEntry.window_seen: bool = False` through `_entry_for` and restore, so a restart neither forgets that a browser had a window nor invents one.
`close_and_forget` is untouched and reachable only through `DELETE /browsers/<name>` (the fleet CLI's `close`), which the skill now describes as "rarely needed: closing the window stops the browser and keeps your logins".

**What the user sees.**
Closing the last browser window stops the browser as soon as the shell's hint lands, and within one sweep interval when it does not.
Opening the browser shortcut, the launcher tile, or `layout.py open browser` brings the same browser back at `/?session=browser-1`, with its tabs, logged in.
A window that exists while the browser is stopped (an agent stopped it, or the user did from the viewer) keeps showing the viewer's stopped overlay with its Start button, as today.

**Several windows on one browser** work today and stay: each window is a viewer of the same display, up to the fleet's eight, and any one of them keeps the browser alive.
A second user on their own desktop opens their own window on the browser through the shortcut.

### 4.5 The chat and the files app

Neither declares a window-bound resource and neither sweeps.
Closing a chat window destroys nothing; a chat is destroyed only from its own list.
Closing a file viewer window destroys nothing.

### 4.6 The close hint

The manifest gains an optional `window_closed_path` (a launch-path-shaped value: rooted, no query string).
`forward_port.py` copies it onto the registry row and `RegistryRow` reads it; a row without it means the app wants no hint.

Whenever a window of an app closes, for any reason (the close control, the window and taskbar menus, the minds close chord, `layout.py close`, a desktop's deletion), the shell POSTs `{"path", "window_id", "desktop_id"}` to the app's registered URL plus its `window_closed_path`, from a daemon thread, with a 2 second timeout, after the close has been written and broadcast.
A failed post is a debug log; the shell never waits for, retries, or acts on the answer, and the close is complete whether or not the app is up.
The app's handler marks the resource the body's `path` names window-seen (the post is proof a window showed it), runs a sweep, and answers 204; what is shown now is read from the shell's desktops, never inferred from the body.
The terminal and the browser declare `window_closed_path = "/api/window-closed"`; the chat and the files app declare none.

## 5. Part C: agents open windows

### 5.1 The op route

`open` (contracts section 8) gains one argument:

- `minimized` (bool, default false): a window this open creates has the requesting client's placement written minimized instead of shown.
  A window this open finds instead (`if_present: focus`) is left exactly as placed, neither raised nor minimized, so an idempotent open from an agent never pulls a window the user put away back over their work.
  `layout.py open ... --minimized`.

And one relaxation of the targeting rule, for `open` only:

- when neither `args.client`, the requester's last messaging client, nor a single connected client settles the target, `open` does not answer 412.
  It writes the window on `args.desktop` (else the first desktop) with no placement, so it appears minimized for every client, and answers `{"ok", "desktop_id", "client_id": null, "desktop", "layout": null, "window_id"}`.
  Every other op keeps the 412, since they edit one client's placements.
  `ShellState.open_window` takes an optional client for this; the HTTP window route (`POST /api/desktops/<id>/windows`) keeps requiring one, since a browser always knows its own id.

### 5.2 The fleet CLI and skill

`_open_viewer_window` passes `--minimized`, so the browser appears in the taskbar rather than over what the user is doing, and the fall-back hint goes away (an open with no client now succeeds).
The skill's opening section says: the fleet has one browser; `new` gives it to you (starting it if it was stopped) and opens its window for the user; the browser lives as long as a window shows it, so do not close the user's window and do not expect a browser nobody has a window on to outlive a sweep; `close` deletes the profile and is rarely what you want.
The `2/2 browsers open` prose goes.

### 5.3 The manage-desktop skill and `layout.py`

- `close` is documented as ending a terminal or stopping the browser once no window shows it.
- The refusal text for `delete` in `layout.py` names the sweep instead of "the terminal offers no delete route yet".
- `open --minimized` and the no-client behaviour are documented.

## 6. Contract and document changes

- `contracts.md` section 2: the `window_closed_path` field and the built-in manifests table of section 3.1; section 3: the registry key; section 5.3: the close's post; section 8: the `minimized` argument and the `open` exception to targeting.
- `plan-desktop-interface.md` 3.6 (seeded modes), 4.5 (closing: "what the window showed is the app's to keep or collect; the terminal and the browser collect it once no window shows it"), 9.2 and 9.4 (the terminal's and browser's lifetimes, one browser), 15 (the close hint deferred).
- `concepts.md` decisions: 9 amended (close removes the window for everyone; whether the app keeps what it showed is the app's rule), and decisions 15 to 18 added from section 2 of this spec.
- `system/apps/system_interface/README.md`: no change beyond the shortcut sentence.
- Terminal and browser READMEs, the browser runner's module docstring, both skills, and the changelog entries of `terminal`, `browser`, `chat`, `app_manifest`, `system_interface`, `.agents`, and `system` (this document).

## 7. Testing

- `app_manifest`: the reader against a threaded fake shell (shape accepted, wrong shape and connection refused answering `None`); `manifest_test.py` and `registry_test.py` for `window_closed_path`; `forward_port_test.py` for the copied key.
- Shell: `state_test.py` or `routes_test.py` for the close's post against a recording fake app (posted on close and on desktop deletion, not posted for an app without the field, a refused or absent app not failing the close).
- Terminal: `sessions_test.py` cases for `sweep_windows` over the fake tmux (seen then collected, never seen left alone, stopped record forgotten, agent session refused, unparsable path names nothing); `pages_test.py` for the hint route waking the sweep; `main_test.py` or `test_terminal_app.py` for the thread's start and stop.
- Browser: manager tests for the cap of 1 on create and restore, `/new` on a running, stopped, and absent browser, the sweep stopping a seen browser and leaving an unseen one, and the flag round-tripping through the manifest.
- Shell: `desktop_routes` and `test_layout_pipeline.py` cases for `open --minimized` and for an `open` with no client (window written, no placement, `client_id` null).
- `system/test_app_manifests.py` for section 3.1; the frontend's `ShortcutIcon.test.ts` already covers the label rule.
- Manual, in the staging workspace: a terminal opened from the shortcut dies right after its close; the browser stops on close and comes back logged in from the shortcut; an agent's `agentic-browser-fleet new` shows a minimized browser entry; closing that window stops the agent's browser.

## 8. Implementation order

Each step leaves the tree green and is one or two commits.

1. Shortcuts: the four manifests, the chat's `root` launch path, the label rule, the popover default, `test_app_manifests.py`, contracts section 2.
2. `app_manifest`: the `window_closed_path` field and registry key, `forward_port.py`, the window reader and `shell_base_url`.
3. The shell's close hint.
4. The terminal sweep and hint route.
5. The browser: the cap, `/new` and `POST /browsers` semantics, restore under the cap, the sweep and hint route, the manifest flag.
6. The op route: `minimized` and the no-client `open`; `layout.py`; the fleet CLI's `--minimized`.
7. Skills, READMEs, plan and concepts amendments, changelogs.
8. Sync into the staging workspace (Python changes to the terminal, browser, and shell: `supervisorctl restart` of those programs, announced first); the existing desktops keep their shortcuts, a fresh desktop gets the new ones.

## 9. Out of scope

- A page asking the shell to close its own window (a terminal whose shell exited, a browser an agent closed).
  Needs a new page-to-shell message; the viewer's overlays cover it for now.
- One Chromium with one profile and a window per fleet browser (shared logins across concurrent browsers).
  Moot while the fleet holds one browser.
- Hiding the chat's `new` tile from the launcher: resolved by the launcher-and-getting-started plan, where `new` is the launcher's primary free-text row rather than a tile.
- Migrating existing desktops' shortcuts.
- Letting only the last-interacted window of a shared browser drive its size; every viewer's resize wins in turn, and the others letterbox, as today.

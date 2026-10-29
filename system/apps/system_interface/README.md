# System Interface

The workspace's shell: its window manager and app management. It serves one
document (`/`, the desktop) that arranges windows, keeps desktops, and
manages apps, and it knows every app only through the desktop interface's
contracts: a manifest, a registry row, and the browser-side contract module.
This package imports nothing from mngr or from any app, never runs the `mngr`
binary, and names no app (`test_project_ratchets.py` holds all three).

## Model

The shell speaks the vocabulary of the desktop interface. The spec,
`docs/system/blueprint/desktop-interface/plan-desktop-interface.md`, is the
reference; `contracts.md` beside it holds every route, message, and file
format. In brief:

- An **app** is a supervised program with a manifest (`app.toml`), a row in
  the registry (`data/.state/apps.toml`), and its own browser origin. It is the
  unit you install, stop, start, and share. Its manifest declares **launch
  paths** with documented params: a GET launch path is a page opened with
  the params as its query, a POST launch path (`new` at `/new`, say) is
  posted the params by the shell and answers the page to open; an app that
  declares none offers `open` at its root.
- A **desktop** is a named, shared collection of windows and shortcuts over a
  wallpaper, owned by convention (the multiplayer spec, section 8.1): the
  owner's carry no owner key, a visitor's carry theirs. Everyone sees the same
  desktops and the same windows on them; a signed-in visitor gets a desktop of
  their own, seeded from the first one, the first time they arrive (see "Who
  is here"), and looking at someone else's desktop is switching onto it with
  the viewing chrome on (see "The desktop, the taskbar, and the launcher").
- A **window** is one page of one app on one desktop: the app, the path under
  its origin the page is at, and the title the page last reported. That is
  the whole of what the shell knows about what a window shows.
- A **placement** is where one client keeps one window: its frame in fractions
  of the backdrop, whether it is snapped or maximized, whether it is
  minimized, whether it is pulled out into a desktop window of the Imbue Studio app's
  own; the order is the stack. A **client** is one browser context,
  identified by a stored id, with an active desktop. Truth is shared,
  arrangement is scoped.
- A **shortcut** is an icon on a desktop's backdrop that runs one app's launch
  path, in `focus` mode (raise the app's most recent window, opening one only
  when it has none) or `new` mode (always open one).
- Titles come from the pages, icons and launch paths from the manifests, and
  liveness from supervisord. The shell stores no status and no recency of its
  own.

## What the process serves

The `system-interface` tool (the shell's own uv tool environment, run by
supervisord from the repo root) listens on `http://127.0.0.1:8000` and serves:

- `/` and the SPA catch-all: the desktop, built into
  `imbue/system_interface/static/`; `/assets/<path>` for its bundle.
- `/api/health`: `{"status", "is_frontend_built"}`, the probe the update
  apply and the preview flow poll.
- `GET /api/schema`: the OpenAPI document of the shell's routes, built from its
  url map (`system/libs/app_schema`); the manifest's `api_schema_path`.
- `/_static/app_contract.js` and `/_static/context_menu.js`: the browser-side
  contract module and the element context menu module (sources in
  `system/libs/workspace_ui/src/app_contract.ts` and `context_menu.ts`), built
  into this app's static output; every app serves those same files from its
  own origin, since a cross-origin module import carries no cookie and the
  forwarder refuses it. The desktop's own chrome draws the element menu too: a
  right-click the views leave alone opens it, and the entry, shortcut,
  backdrop, and overview thumbnail menus end with its reference rows
  (`docs/system/blueprint/element-reference-menu/`).
- The shell routes of contracts sections 5 and 8: desktops (`/api/desktops`,
  `/api/desktops/order`,
  `.../<id>/settings|wallpaper|delete|shortcuts|shortcuts/move|shortcuts/remove`),
  windows (`/api/desktops/<id>/windows`, `.../windows/<window>/close|location`),
  placements (`/api/placements/<desktop>`), wallpapers (`/api/wallpapers`,
  `/wallpapers/<kind>/<name>`), the per-app Stop, Start, and Quit
  (`/api/apps/<name>/stop|start|quit`), clients (`/api/clients`, and the arrival
  `/api/clients/<client>/arrive` a page posts first), client activity
  (`/api/client-activity`), the inventory (`/api/inventory`), each client's
  pinned-entry presentation
  (`/api/clients/<client>/entries/<app>`), what a client's phone shows
  (`/api/clients/<client>/shown`), the avatar (`/api/avatars`,
  `/api/avatars/<id>/image.svg|source.svg|icon.png`, `/api/avatar-selection`; the
  registration `POST /api/avatars` is loopback-only), the embedder-message
  relay (`/api/embedder-messages`), and the loopback-only op route
  (`/api/layout/broadcast`).
- Workspace themes (`docs/system/blueprint/workspace-themes/`): the catalog
  with the default the workspace wears (`/api/themes`), the default's route
  (`POST /api/themes/default`), each theme's icons
  (`/api/themes/<id>/icons/<file>`), a desktop's own theme
  (`POST /api/desktops/<id>/theme`), the theme files every app serves from its
  own origin (`/_static/themes/<path>`), and the theme gallery page
  (`/theme-gallery`), which shows every part of a theme. A watch on the theme
  folders and the app manifests says `themes_changed` when the catalog does.
- The home-screen tile: `/apple-touch-icon.png` and `/manifest.webmanifest`,
  from the selected avatar design and the workspace's name, which also titles
  the page.
- Presence (`/api/presence`, `/api/presence/heartbeat`) and the roster
  (`/api/roster`): who is connected right now, with their identity and
  profile, and everyone who may be here (see "Who is here").
- The show-around tour's record (`GET` and `POST /api/tour`, `{"is_seen",
  "resume_index"}`): whether the tour of the desktop
  (`frontend/src/model/tourCards.ts`) has run in this workspace, and the card it
  resumes at. A new workspace (one the bootstrap marked with `offer_tour` on its
  first boot) runs it by itself the first time its agent goes to work (so the
  owner learns the product while waiting for a reply; the page's
  `tour_start_ms` query parameter makes it wait for a task that is taking a
  while instead); the agent answering interrupts it, and the next time the
  agent works it resumes from the card that would have come next. The
  launcher's "Show me around" row runs it again from the first card any time.
  A card about a control of the Imbue Studio chrome asks the chrome to ring it
  (`minds:highlight-chrome`); the chat card's Next snaps the pinned chat to the
  right half of the desktop, revealing the shortcuts.
- The WebSocket (`/api/ws`): `apps_updated`, `desktops_updated`,
  `presence_updated`, `roster_updated`, `placements_updated`, `active_desktop_changed`,
  `client_entries_changed`, `avatar_status`, `avatar_selection_changed`,
  `themes_changed`, and `layout_op` (contracts section 6); it accepts each
  client's `client_state` report.

Its state lives under `data/.state/system_interface/`: `desktops.json`, `tour.json` (whether the tour has run, and where it resumes), `offer_tour` (the bootstrap's mark on a new workspace, without which the tour never starts by itself),
`desktop_themes.json` (the workspace's default theme and each desktop's own),
`default_shortcuts_offered.json` (the apps whose default shortcut the shell
has offered), `placements/<desktop>/<client>.json`,
`window_paths/<client>.json` (a client's own paths for independent windows),
`clients.json`, `users.json` (the desktop made for each visiting user),
`avatar_selection.json`, and the client-activity
event log (`events/client_activity/events.jsonl`, what `workspace-layout context`
reads). Wallpapers are listed from `static/wallpapers/` (bundled) and
`data/.apps/system_interface/wallpapers/` (files the user adds); avatar designs
an agent registers live in `data/.apps/system_interface/avatars/catalog.json`
beside the seven bundled ones (`docs/system/avatar-designs.md`). Presence lives
beside it under `data/.state/presence/` (`--presence-dir`): the per-user files
and the profile cache.

### The desktop model

- **Records** (`shell/data_types.py`): a `Desktop` (name, wallpaper,
  shortcuts, windows, and its owner by convention; the colour and glyph of the
  retired switcher stay in the record), a `Window` (an app, a path
  under its origin, and the title its page last reported; shared), and per
  client a `DesktopLayout` of `WindowPlacement`s (frame in fractions of the
  backdrop, state, minimized, detached; the order is the stack).
- **Pinned windows** (`docs/system/blueprint/pinned-taskbar-entries/`): an
  app whose manifest declares a `[pin]` has exactly one pinned window on
  every desktop, reconciled on every read after the registry is read and on
  every change of the registry's rows, and never closed. With the
  `independent` scope the window's shared path stays its home path and each
  client's own path and title live in `shell/window_paths.py`'s per-client
  file. How a client shows the entry (in the bar or floating, plain or as the
  avatar) is on its client record.
- **The avatar** (`avatar/`): the bundled and registered designs, the
  workspace's selection, the rendered image routes, and the status reader,
  which folds mngr's agents event file and the chats' background task markers
  into a mood (working when any agent but the services agent is running, or
  any chat is busy on a background task that will wake its agent) and pushes
  `avatar_status` on change.
- **State files**: a fresh workspace gets one desktop, `Home`, seeded from
  every registered app's default shortcut (its `default_shortcut`, else, for
  an app a program runs, its first launch path taking no text and no mention,
  or its first when every one takes one) on the first read after the registry
  has been read.
  An app that registers later has its default shortcut added to every
  desktop once, on the registry change and on every read;
  `default_shortcuts_offered.json` lists the apps offered so far, so a
  shortcut the user removed stays removed. A client record holds the client's
  active desktop, when it was last seen, and the user it last arrived as;
  clients unseen for a while are pruned with their placement files.
  `users.json` holds the desktop made for each visiting user (see "Who is
  here").
- **The pure editor** (`shell/desktop_document.py`): every verb (open, close,
  focus, minimize, restore, maximize, snap, place, detach, reattach, the
  shortcut edits) and
  every geometry rule (cascade, fit, snap zones, un-snap, the grid, nearest
  free cell, reading order, shortcut placement) as pure functions over the
  records. The rules the frontend also applies pass the shared vectors in
  `docs/system/blueprint/desktop-interface/geometry_vectors.json`. Whose a
  desktop is, and the reorder that moves only the requester's own, are the
  pure functions of `shell/ownership.py`, which the editor stays apart from.
- **Routes** (`shell/desktop_routes.py`): the desktop, window, placement,
  arrival, and wallpaper routes above. A desktop create takes the name
  (the shell picks the rest) and writes the requester's key as its owner
  (none for the workspace owner); `POST /api/desktops/order` lists the
  requester's own desktops in a new order and leaves the others in place,
  refusing an order that is not exactly their own. A placements save carries a save id
  and the stamp it was based on; a save over a newer arrangement is refused
  with 409 and the browser refetches. `GET /api/inventory` is
  `{desktops, apps, clients}`, each `app` carrying its `launch_paths`,
  `default_shortcut`, and `is_running`.
- **The op route** (`shell/desktop_routes.py`, over the request models of
  the `workspace_layout` library's `ops`): an op is `{op, args, requester}`,
  the requester `{app, marker}` or null; `self` names the requester's app's
  window whose path carries the marker. The document verbs (`open`, `focus`,
  `minimize`, `restore`, `maximize`, `place`, `close`, `navigate`, `load`,
  `show`, the shortcut and wallpaper edits) are applied to the files and
  announced as `desktops_updated` and `placements_updated`; `context` answers every
  client's recent activity, folded from the client-activity log and the live
  socket registrations, and `desktops` and `list` answer the inventory
  document (`GET /api/inventory`'s `{desktops, apps, clients}`);
  `refresh` and `reload_system_interface` reach the browser only as
  `layout_op` messages, and `focus`, `show`, and an `open` that is not
  minimized also send one telling the target client which window they put in
  front.

The backend is the `imbue/system_interface/shell/` subpackage (inventory and
liveness, desktops, placements, wallpapers, clients, users and the request
identity, client activity, the pure desktop document editor, the
routes with their shared helpers, state); the package root holds the process
(`main.py`, `server.py`), the not-built placeholder, and the update-staleness
check. The frontend
(`frontend/`) is one member of the npm workspace rooted at
`system/package.json`; the design system, the base helpers, and the contract
modules it shares with the app pages live in `system/libs/workspace_ui`, and
`src/relay.ts` is the shell's side of the embedder relay (it forwards the
framed pages' `minds:` messages to the minds chrome unchanged, and hands on
the chrome's messages of types the embed contract does not define, which the
contract's endpoint in `workspace_ui`'s `embed.ts` drops).

A message the shell's page receives, from the Imbue Studio chrome or from an
app's page (`shell:message`),
reaches every app whose manifest registers its type (`[[message_handlers]]`):
the shell's page hands it once to the shell's backend (`POST
/api/embedder-messages`, `shell/embedder_messages.py`), with the client whose
page received it (a chrome message of one of the contract's own types through
`embed.ts`'s observer, any other, such as a notification's `destination`,
through `relay.ts`). A solo page hands on its apps' messages but never the
chrome's, which the main window's page already relays. A `path` handler has the
message posted to the route it named; a `show` handler has the shell build the
page its template names from the message's fields and show it through the op
route's `show` below, in-process. A message some app did not take becomes a
notice carrying that app's reason, and a page shown while the client has only
pop-outs open becomes a notice to reopen the main window. The shell's page
also opens links itself: it announces `opensLinks` and `opensCustomSchemeLinks`
to the chrome and opens a `minds:open-link` (a popup Imbue Studio turned back
into the workspace) or a
page's `shell:open-link` by the shared link classifier (`workspace_ui`'s
`links.ts`): a `file:` URL as `open:file`, a local URL on an openable app's
`<label>.localhost` (or `<name>.localhost`) host, or else at its registered
backend port, as that app's window (`windowAtLocalAppHost` and
`windowAtBackendUrl` in `frontend/src/model/pageUrl.ts`), any other local URL
as `open:url`, one of this workspace's app addresses (its addresses on the
domain the workspace was last shared under included, read off the apps'
`share_url`) as that app's window, and an external web, `mailto:`, `tel:`, or
custom-scheme (`slack:`) link as `open:web` or `open:<scheme>` (`open:mailto`,
`open:slack`) to the app registered for it, or else outside the workspace: back
to a chrome that says it opens that kind of external link
(`minds:open-external`), else in a new browser tab; a custom-scheme link
facing an older desktop app, which shows a popup of one as an empty window,
opens by navigating the page to it instead. An app's window already
there that the client has popped out is raised in its own window, and one a
link opens while the client has only pop-outs open gets the same notice. An
app that wants a window for what it was told asks the op route's `show`, which
takes the app, a path, the other paths that count as already showing it, and
the pages whose windows it may point at the path, and picks the window itself:
one already showing it (switching desktops if it must), else the frontmost
window on screen at one of those pages (pointed at the path), else the app's
pinned window, else a new one. A pulled-out window it settles on stays out, and
the client stays on its desktop: the shell asks that client's pages to have the
Imbue Studio app raise the window's own desktop window (the window's own solo
page asks too, so the raise works with the main window closed). The shell reads
no meaning into a path's query string. The other ops treat a pulled-out window
the same way: `focus` raises it, and the ops that would move it back onto the
desktop are refused unless forced (desktop contracts.md section 8).

### How the shell learns about apps

The **inventory** (`shell/inventory.py`) watches the registry, probes each
app's liveness (supervisord for rows with a `program`, a TCP connect
otherwise) on a periodic sweep, and pushes the diffed result to every browser
as `apps_updated`. That is all it knows of an app: its row (display name,
icon, launch paths, default shortcut, launcher rank, message handlers) and
whether it is running.
The page learns the apps from `GET /api/inventory` right after it arrives (one
read for the desktops, the apps, and the clients), so it never draws a
shortcut for an app it does not know; the socket's `apps_updated` (sent on
every connect, and on every change) keeps the list current from then on.
Until that first read answers, a shortcut whose app the page cannot look up
draws faint as "Connecting to the workspace..." and running it says the page is
still connecting; only once the apps are known is a missing app reported as
not registered. The sweep also compares the registry's mtime with the last
read's and re-reads on a change, the backstop for a write no watch event
reported (under gVisor and on lima, a change made outside the sandbox raises no
inotify event in it).

Every registry read is also announced to the minds desktop (`app_announcements.py`):
one `service_registered` event per app whose URL, label, icon, display name, or
`system` or `internal` declaration (which tell the minds Share tab to list the
app under its System group, or not at all) differs from the last announced, and
one `service_deregistered` per app that left, appended to
`$MNGR_AGENT_STATE_DIR/events/services/events.jsonl` in the `imbue_common`
event envelope; the first read after the shell starts announces every app. The
stream is what `mngr forward` and the desktop resolve app origins from, and
they replay it whole each time they attach, so a stream over 5 MiB at that
first read is renamed aside before it and gzipped to
`events.jsonl.<timestamp>.gz` (a name mngr does not replay) on a thread of its
own; the newest three archives are kept. Streams that large were left by the
retired `app-watcher` service, which re-announced every app on each registry
rewrite. A preview shell announces nothing, since its registry is a copy.

The shell also owns each stoppable app's process (`shell/app_lifecycle.py`,
the stop-when-no-windows spec): an app whose manifest declares
`stop_when_no_windows` is stopped once no window on any desktop has shown it
for a minute (only once someone has visited the workspace, and never, unless it
is internal, while a per-app share grant names it, since such a visitor reaches
the app without the shell: `shell/share_grants.py` reads
`data/.secrets/share_grants.toml`), and while any stoppable app is stopped the
shell holds its port (`shell/port_parking.py`), so the first request for the
app starts it again and is answered with a loading page that reloads into the
app. "Quit <app>" on the window menu (`frontend/src/views/WindowMenu.ts`)
closes every window of the app and stops it at once; critical apps offer
nothing there and are never stopped or parked.
The stop and start routes remain for agents. A framed page reaches the shell only
through the contract module (`shell:open`, `shell:focused`, `shell:location`,
`shell:capabilities`, `shell:start-with-text`); a page that reports the path it is showing gets it
stored on its window and reopens there, one that declared `navigation`
is sent `shell:navigate` when an agent points its window elsewhere, and one
that declared `closeChord` keeps its window on the close chord (it is only sent
`shell:close-request`; the browser closes one of its own tabs that way).

### Who is here

Every request that reaches the shell carries the requester's identity in the
`X-Imbue-Identity` header, stamped by whichever proxy admitted it -- the local
`mngr forward` or the share gateway (the header contract is in
`system/services/share_gateway/README.md`): `owner`, on a shared
workspace the account's `user_id` and `email`, and for a visiting agent its
`agent_id`, `agent_name`, and `granter`. Nothing more: what to call an
account and what it looks like is its profile, which the shell fetches from
imbue_cloud (below). The shell page posts `/api/presence/heartbeat` every 30
seconds while the tab is visible, once immediately when it becomes visible,
and nothing when it goes away, carrying when the page last saw keyboard or
pointer input. The page sends nothing about who it is; the heartbeat's answer
is the requester's own identity record and the participant key its entries
carry (the multiplayer spec's section 4.1: `user:<user_id>`, or `owner` for
the owner of a workspace that has not shared, who reveals no account until
they share), which is what the account affordances render. A visiting agent
never heartbeats (its presence is the gateway's last-seen record); its
request answers 204.

Each person has one file, `data/.state/presence/users/<id>.json` (`<id>`
their user id, or `owner`), holding their identity snapshot (`user_id`,
`email`, `owner`, `first_seen`) and the newest input time a page of theirs
reported (`last_input_at`), written on first sight and rewritten when any of
those change; a heartbeat just touches the file's mtime. A person is
connected while that mtime is within 70 seconds (two missed heartbeats plus
slack), and the mtime is their last seen; they are active while their newest
input is within 60 seconds and idle otherwise. Files are never removed:
whoever is not connected is still the record of who was last here, and when.
Apps read the directory the same way. The shell sends the connected set on
every WebSocket connect and pushes `presence_updated` -- one entry per
account, however many tabs; the `owner` record names none and is left out --
when a heartbeat brings someone in and, from a sweep every 10 seconds, when
someone's heartbeats have stopped. The taskbar's tray draws the roster (below)
rather than the connected set: one picture per person, faded while they are
away, the viewer's own entry last, the own entry being the one whose
participant key the heartbeat's answer names. A changed name or profile picture
reaches the tray through the profile cache (below) within five minutes. A
visitor granted a single app never loads the shell and so never appears: they
are in one app, not in the workspace.

**The roster.** Everyone who may be here, whether or not they are, is the
roster (`roster.py`; `GET /api/roster`, and `roster_updated` on the socket on
connect, with every `presence_updated`, and whenever one of its files
changes): the owner first, then every account the workspace-level grants of
`data/.secrets/share_grants.toml` name, then every invited email still in
them, then every visiting agent of `data/.secrets/agent_access/grants.toml`
(both parsed by the `share_grants` library the gateway parses them with), and
the workspace's own agent last, named after the workspace and pictured by the
selected avatar. Each entry carries its key, kind, name, picture, email, the
handle it is mentioned by (`@agent` for the workspace agent, reserved first;
a later collision gets `-2`, `-3`, ...), its colour (one of eight, by the
key's hash), and whether it is here: a person by their heartbeats, a visiting
agent by the gateway's `data/.state/agent_access/last_seen.json`, under the
same 70-second window. The apps read the roster from here rather than from
its files: it is the one place names, keys, and handles are decided.

**Profiles.** A user's display name and profile picture come from imbue_cloud,
not the header: the shell (`profiles.py`) fetches `GET {broker_url}/users/<user_id>/profile`
(public; `{"user_id", "display_name", "profile_picture_url"}`) with a 2 second bound,
where `broker_url` is `SHARE_BROKER_URL` in `data/.secrets/share.env`, the
file the Imbue Studio desktop writes while the workspace is shared (read fresh on
every miss; no file means no profiles). Each answer, and each failure, is
cached for 5 minutes under `data/.state/presence/profiles/<user_id>.json`, so
a connector outage costs one failed fetch per user per 5 minutes and no
request ever hangs or fails on it. The profile rides on each present user
over the wire (`display_name`, `profile_picture_url`, null when there is none)
and names a visitor's desktop on their first arrival. The picture itself is
served from the shell's own origin, since the pages' content security policy
admits no outside image: the roster points at
`GET /api/roster/<key>/picture?v=<version>`, which fetches the bytes once from
the profile's URL (5 second bound) and keeps them under
`data/.state/presence/profiles/<user_id>.picture.json` until the profile names
another URL; `v` is a digest of that URL, so a newer picture is a newer address
and an answer under the current version is cached by the page for good (one
under an older version, or none, is the same bytes uncached).

The same header decides where a page lands. A shell page posts its arrival
(`POST /api/clients/<client_id>/arrive`) before it reads the inventory
(`GET /api/inventory`: the desktops, the apps, and the clients in one answer,
so the seeded desktop is in it). The
owner, and any request without a user id, land on the client's recorded
desktop, else the first. A signed-in visitor would otherwise land on the
owner's desktop and open and close the owner's windows, so on their first
arrival the shell makes them a desktop named after them (their profile's
display name, else the email's local part, else `Guest`, made unique; the
profile is resolved before the arrival takes the state lock), seeded from the
first desktop: its shortcuts, its wallpaper, and a new window at the path of
each window open there. It is remembered in `users.json`, every later client
of that user lands on it, a returning client keeps the desktop it was on (when
it last arrived as that same user), and if it has been deleted the next
arrival seeds another and says so. Every desktop stays shared and reachable
from everyone's launcher; there is no private mode. The seeded desktop is
written with the visitor's key as its owner (the multiplayer spec, section
8.1), so their card and their overview list it as theirs; a desktop whose owner
has since left the grants is left behind, listed in the owner's overview under
"Left behind" so it can be viewed and deleted.

### The desktop, the taskbar, and the launcher

The backdrop shows the active desktop's shortcuts and windows; each window is an
iframe of an app page under a title bar with the window menu, Refresh, and "Chat
about this app" on the left while it is focused, the page's title centred, and
the window controls on the right. The taskbar shows the launcher field, the
overview button, one entry per window of the active desktop, and the tray. A
pinned window's entry is a plain entry like any other window's, except that its
Close closes the window for this client alone (minimized, and the entry out of
this client's bar until something shows the window again); the entry's context
menu moves the app's creature -- the workspace's avatar, docked in the tray by
default -- between the tray and a spot floating above the windows, and opens the
avatar chooser. Shortcuts are added, moved, and removed on the backdrop.

**Desktops** (the multiplayer spec, section 8; `frontend/src/views/Overview.ts`,
`model/Ownership.ts`). The overview button (four squares, "Manage Desktops")
opens the overview over the backdrop: the active desktop's windows as live,
inert tiles scaled into a grid (the live-page layer lays each window's page
over its tile; a minimized window's tile is faint), a click raising one, and
below them one thumbnail per desktop the viewer owns, drawn from its wallpaper
and this client's placements of it, the active one outlined. A click switches,
a double-click on the name renames, a drag sideways reorders (`POST
/api/desktops/order`), a right-click offers "Delete desktop...", confirmed in
place and refused for the last desktop, and the plus makes "Desktop N" with
the seeded shortcuts, owned by the viewer. The owner also sees the desktops
left behind by people no longer in the roster, after a thin rule. Escape or a
click on the bare backdrop closes it. The backdrop's own context menu offers
"New desktop", "Show all desktops", and "Change wallpaper...", which opens the
wallpaper chooser (`WallpaperChooserDialog.ts`, what the settings dialog
became: a desktop's colour and glyph are no longer shown or edited). A person's
tray card lists the desktops they own, and a click on one of somebody else's
*views* it: the client switches there with the viewing chrome on, an outline
in the owner's colour around the whole desktop, the taskbar included, and a
chip in that colour with their picture, "<Name> · <Desktop name>", and "Stop
viewing" in place of the tray, the overview button hidden meanwhile (and the
backdrop menu's "Show all desktops" disabled). Stop viewing goes back to the
desktop the window came from, else the viewer's first. A viewed desktop is
shown as its owner has it, live: the layout read is the owner's newest
client's written one (`?of=owner` on the placements read), refreshed on every
`placements_updated` push for the desktop and by a slow backstop poll, and it
is read-only -- the pages are inert, the windows take no press, the taskbar's
entries mirror theirs without acting, and nothing a viewing window holds is
saved as the viewer's layout. On every other client the owner of
a desktop somebody else is on wears a ring in their colour in the tray, read
off the clients and the desktop moves the socket announces; a move by a client
the last read did not list, and a change in who is here, read the clients
again.
The launcher's window rows name the owner of a desktop that is not the
viewer's. The phone layout offers no overview.

**App chat** (the multiplayer spec, section 9; `frontend/src/views/TitleBar.ts`,
`store/DesktopStore.ts` `openAppChat`). Every window of an app that is not
internal carries "Chat about this app" (a speech balloon) in its focused title
bar, the chat app's windows included. It runs the first launch path of the
machine that declares `app_params` (the chat app's `app-chat`, which names the
window's app and its display name in the params the path declares) through the
launch route's `show` target: the shell shows the answered page as the `show` op
would (a window already at the path raised, else the pinned window pointed at
it, else a window opened) and lays the two windows out for this client, the chat
snapped to the half of the backdrop its frame's centre was already on (the right
when it is minimized or was never placed) and the app's window to the other, the
chat on top; the pinned window's own balloon, whose show navigates that very
window, lays nothing out, and neither does a show whose window, or the window
beside it, is pulled out into a window of the chrome's own. The chat app answers
the chat about the app: the one labelled `app_chat` with the app's name (so a
renamed app keeps its chat), else the one titled "<App display name> Chat", made
with both on the default provider when there is none (so the chat app's own
windows open "Chat Chat"), with a pending intake whose `hint` the chat root puts
on the composer as its placeholder, "How do you want to change <App>?", and
focuses. The shell names no app in any of this.

**The tray** (the multiplayer spec, section 6; `frontend/src/views/SystemTray.ts`
and `views/tray/`) is where the people and the agent live: the share button (a
dashed circle with a plus, drawn only under an Imbue Studio chrome that has said
what it can do, which it asks to open the share panel on the whole workspace
through `minds:open-share-settings` with no service named), one 32px picture per
roster entry in roster order (a profile picture, else a monogram on the person's
colour; a dashed envelope for an invited email; a monogram tile for a visiting
agent; a neutral silhouette for the owner of a workspace that has not shared;
faded while the entry is not here; stacked from four entries on, each
tucked under the one before it, spread while the pointer is over them; hopping
once when it arrives and when a bubble of its appears), the viewer's own
picture, and the agent's avatar at 36px in its mood, attending while the pinned
chat window is focused -- the one creature's docked home, not drawn while this
client floats the avatar over the windows instead. Hovering
an entry for 180ms opens its card above it: a person's shows the picture, the name
("(you)", "(owner)"; "You" for the owner of an unshared workspace), the status
line ("Active", "Idle · 4m", "Offline · last seen 3h ago", "Invited"), what they
are looking at (the window at the top of the stack on the active desktop of
their newest client, read from `GET /api/clients` as the card opens), and their
desktops (every desktop is the owner's until the desktops phase gives them
owners), with "New desktop" on your own; a visiting agent's shows its name,
"Visiting agent", and the status line. The agent's card shows the face, the
workspace name with a pencil that opens the avatar chooser, "Working" or
"Idle", and the chats under "Working on" (status working or background) and
"Needs you" (attention), a click showing that chat through the same
`minds:focus-chat` relay the Imbue Studio app's notifications use. Clicking a
person drafts a mention of them (`@<handle> `) through the first launch path of
the machine that declares a `mention_param` (the chat app's, into the Lounge),
into its pinned window on this desktop when there is one; clicking the agent
runs the primary text action with nothing to send, which opens the main chat;
clicking your own picture does nothing. The shell names no app in any of this.

The chat app feeds the tray over loopback: `POST /api/bubbles` with every message
somebody sends and every turn the agent closes with text (`{"speaker", "chat_id",
"chat_title", "text", "message_id", "mentions"}`), which the shell broadcasts as
`bubble` to every window but the speaker's own clients (the clients whose records
carry the speaker's key; the owner's under either of their keys, the account's or
the synthetic `owner`, whichever the bubble carries; a window looking at the
bubble's chat drops it on receipt unless it mentions the viewer); and
`POST /api/chats-activity` with what every chat is doing (`{"chats": [{"chat_id",
"title", "status", "kind"}]}`), kept and broadcast as `chats_activity`, and sent on
connect once reported. A speech bubble is a rounded card hanging above the tray
with an arrow at the speaker's entry, holding what was said, a dismiss X, and a
click that opens the chat it was said in; it lives five seconds after its last
change, a person's new message replaces their bubble, the agent's new message
within its bubble's lifetime is appended (folding past three lines into "and N
more"), bubbles of different speakers stack upward in arrival order, and a mention
of the viewer wears an accent border and stays until dismissed or its chat is
opened (`model/Bubbles.ts`, a pure reducer; the timers are the store's).

The launcher is a text field ("Open an app or send a message") and the menu it
opens above itself (`frontend/src/views/LauncherMenu.ts`, its rows computed by
`reducers/launcherRows.ts`). The rows: one per launch path of every
non-internal app, the apps that declare a `launcher_rank` in their manifest
first in rank order and the rest after them; while typing, one per window of
every desktop (by title, the active desktop's first; choosing one switches
desktop and raises it); and at the foot the free-text rows, one per launch
path that names a `text_param` or a `draft_param`, which run that launch path
through the shell's launch route (`POST /api/desktops/<id>/launch`) with the
typed text as the param (`model/launch.ts`): a GET launch path opens at its
path with the params as the query, a POST one is posted the params (and the
client id, desktop id, and window path as its envelope) and opens at the page
it answers. The first free-text row is the primary action (Enter with no other
row highlighted) and the second the secondary (Ctrl+Enter, Cmd+Enter on
macOS); a free-text row of an app with a pinned window points that window at
the page rather than opening a new one (this client's view alone when the
window is independent), and a text whose encoded GET path would pass the
window path bound is disabled with its reason. One row is always
highlighted; the arrows move it, hovering moves it, Enter or a click runs it,
and a run closes the menu and clears the field. A framed page starts a chat
without naming the chat app through `shell:start-with-text`, which the shell
answers by running the primary free-text row (the Getting Started app's
intents and templates use it), and drafts a text into the chat on screen
through `shell:draft-text`, which the shell answers by running the pinned
app's launch path with a `draft_param` into the pinned window, as the avatar
dialog's "Design your own..." does (the element context menu's "Explain..."
uses it). The shell names no app in any of this. A
fresh install lands on its `Home` desktop with the Getting Started window
open, placed there once by that app for the first client that connects.

A refused operation (an open, a close, a shortcut move) says so in a toast at
the foot of the screen (the shared toasts of `system/libs/workspace_ui`) rather
than a browser alert.

### The phone layout

A phone-sized viewport (at most 500px one way and 1000px the other) gets the phone layout
(`frontend/src/views/phone/`, `docs/system/blueprint/desktop-interface/plan-phone-interface.md`)
over the same store: a bar of home, a pill naming what is on screen, and plus; a
home grid of the apps on the first desktop's wallpaper; a windows sheet listing
every desktop's windows, what this phone showed first; and a start sheet that is
the launcher's menu made tappable. It shows one window at a time and writes no
placement for it, so a laptop sees nothing move: what it shows is recorded as the
client's `shown_history`, where a reload lands. Every window it opens goes on the
first desktop, placed minimized for itself alone, and it follows the `show`,
`open`, and `focus` ops an agent aims at it. Only the shown window's page and
the pinned chat window's are mounted (`pages/livePages.ts`, the `shown` mount
policy).

## Running and developing

```bash
# Backend, from the repo root (the registry path and the state directory
# default are relative to it; run elsewhere, the shell finds no registry and
# lists no apps)
uv run system-interface

# Frontend (with hot reload)
cd system/apps/system_interface/frontend
npm install
npm run dev

# Build the bundle into imbue/system_interface/static/
npm run build
```

The build's `postbuild` step stamps the output with three `git rev-parse`
tree hashes (this frontend directory, the shared `system/libs/workspace_ui`,
and the workspace's `package-lock.json`), each as *committed*, not as the
files just built. The update apply compares that stamp against the merged
tree, so commit before building a bundle that will be handed to the apply.

The frontend styles in the markup: Tailwind utilities over a semantic token
layer (`system/libs/workspace_ui/src/base.css`), with shared primitives
(Button, Modal, the input and badge recipes) for repeated looks. Prefer them
when extending the default look; see [`frontend/style_guide.md`](frontend/style_guide.md).
It is a convention, not a rule: a user who wants their interface restyled gets
that, tokens or not.

## Driving the desktop from an agent

An agent inside the workspace arranges the desktop through
`uv run --no-sync workspace-layout` (`context / desktops / list / load / open / show / focus /
minimize / restore / maximize / place / close / navigate / refresh / shortcuts /
shortcut set / shortcut move / shortcut remove / wallpaper`), which names apps
and windows (desktop-interface contracts.md section 8):

```bash
uv run --no-sync workspace-layout desktops
uv run --no-sync workspace-layout context
uv run --no-sync workspace-layout open files --path /home/user/workspace/data/notes/ --desktop Research
uv run --no-sync workspace-layout open terminal
uv run --no-sync workspace-layout place self --state snapped-left
uv run --no-sync workspace-layout navigate win-0123456789abcdef /other/
```

The document ops are applied by the shell to the desktop and to the target
client's placements and announced as `desktops_updated` and
`placements_updated`, so an op lands whether or not a browser is connected.
Every op targets exactly one client (`--client <id>`, else the client that last
messaged the requesting agent while it is connected, else the one connected
client (when a client messaged, only if it is the same user's), else that
messaging client though it is not connected; refused with the clients listed
otherwise); `--desktop` edits that desktop and switches the client to it; `open`
opens a window at `--path` or at a launch path
(`--launch`, `--param`; a bare URL is the browser's `new`; a POST launch path
is posted the params for the page it answers), minimized with
`--minimized` or beside a window (`--beside [window]`, bare the caller's own
chat), which is moved across only where it has to be and never resized unless
it is over half the backdrop wide, and prints the window's id;
an `open` with no client to target still writes the window, unplaced. A close
is posted to the app's registered `window_closed_path`, when it has one, so an
app whose resources live as long as their windows (the terminal, the browser)
can collect at once (`docs/system/specs/window-bound-resources.md`). Only
`refresh` and the interface reload reach the browser as messages. See the
`manage-desktop` skill for end-to-end orientation.

## Updating the running UI

The deployed system interface is the live web UI the user is looking at, so
changes are not applied in place. Its manifest says `critical = true`, which
routes every edit through `update-app`'s careful flow
(`.agents/skills/update-app/references/critical-app.md`): the change is made in
an isolated worktree, **previewed** to the user as a window
(`preview_app.py up --app system_interface --worktree <dir>` boots
`system-interface --preview` from the worktree, over a seeded copy of the live
state directory and a copied registry, and registers it with a labeled
wrapper page as the `system_interface-preview` app; `down` tears it down),
hardened by a background worker at approval, and applied through the general
**update apply** shared with the `update-self` flow:

```bash
python3 .agents/skills/update-self/scripts/update_self.py apply \
    --merge-ref "mngr/update-<slug>" \
    --worker-bundle "system_interface=<work_dir>/system/apps/system_interface/imbue/system_interface/static" \
    --worker-bundle "chat=<work_dir>/system/apps/chat/imbue/chat/static" \
    --keep-rollback-point --pass-scope "data/.tasks/harden/update-<slug>/pass-scope.json"
```

The apply merges the worker's branch, classifies what changed and does only
what is needed (a dependency refresh, the worker's already-built bundles or a
live build, a pre-flight boot of the merged shell and chat on throwaway
ports), restarts the services agent, then probes: the shell's `/api/health`,
the `/api/health` of every critical app the user can open (at the URL its
registry row names, re-read as the app re-registers), and that
the frontend really serves (the "not built" placeholder and an unserved
`/assets` path are both HTTP 200, so the probe reads the `X-Frontend-Built`
header and checks that the module script comes back as JavaScript). Only then
does it ask every open view to reload, through
`system/scripts/refresh_workspace_view.py` (a `reload_system_interface` op on
the loopback-only op route, which reloads the top-level page and every child
frame, plus the Imbue Studio app's own refresh endpoint). On any failure it reverts
the merge as a forward revert commit, restores the pre-apply snapshots it took
before anything destructive ran, and re-confirms health; the exit code reports
the outcome (`0` applied, `2` rolled back, `3` emergency, `1` precondition).
The scripts under `.agents/skills/update-self/scripts/` and that skill's
`SKILL.md` are the reference.

## When the bundle is missing

`static/` is gitignored build output, produced at workspace creation
(`system/scripts/build_workspace.sh`) and by the apply above. Nothing rebuilds
it at service start, so a code refresh that replaces the tree can leave the
backend with nothing to serve. In that state `/` serves a placeholder, and
because the placeholder is a string in the backend rather than part of the
bundle, it still works when nothing else does.

The placeholder is the workspace's general recovery surface, so it hands over a
**terminal** rather than a repair. It embeds the already-running terminal app
in a frame, and suggests creating an agent to do the work if the reader would
rather not:

```
env -u TMUX mngr create --connect --template chat --label user_created=true --message "i'm seeing \"this workspace's interface needs to be rebuilt, can you fix it?\""
```

`--template chat` is what makes the result a chat, and it carries everything
that is not a choice: the shared work directory, the output style, and running
in the workspace tree rather than a worktree of it. It is harness-agnostic --
`output_style` is honored by the claude, codex and pi plugins alike -- so it
neither picks a harness nor can be relied on to.

The rest is where the line departs from what the chat app's
`agent_manager._build_chat_create_command` passes for the same chat, in four
places:

- **`--connect`, against its `--no-connect`.** Someone typing this wants to land
  in the conversation. It is load-bearing rather than decorative: this
  workspace's own `[commands.create] connect = false` is the default it
  overrides.
- **No `--type`, which the builder must pass.** The app is serving a harness the
  user picked from a menu; this page has no such choice to carry, so hardcoding
  one would hand a codex or pi workspace a line that quietly opens claude.
  Omitted, mngr resolves it from `[commands.create] type`.
- **No `--transfer none`, which the builder spells out.** The `chat` template
  already sets it. Unlike the harness this is not the reader's to choose -- an
  agent in a worktree would repair a copy of the workspace instead of the
  workspace -- so a test reads the template and fails if that setting ever
  leaves it.
- **A `--message` carrying the page's own heading**, so the agent opens already
  knowing what the reader is looking at. A test ties the two together, since a
  reworded heading would otherwise leave the message quoting a sentence that
  appears nowhere.

The line also names no agent, so mngr mints one and a second run starts a fresh
conversation rather than colliding with the first. `env -u TMUX` is what lets the
connect half work from the workspace's tmux-backed terminal tabs, which `mngr
connect` otherwise refuses to attach from.

Tests pin the whole of it: the flags against the builder, the line parsed back
into an argv and resolved against the live mngr CLI, the same line word-split by
a real `sh` (because `shlex` expands nothing and a shell does), and the rendered
page's own repair block -- so the suggestion cannot drift into creating something
that is not a chat, into a line that does not run, or into a line other than the
one a reader copies.

A shell rather than a "rebuild" button because a button has to be right about
what went wrong: the states that strand a workspace here are dominated by ones
where a build dispatched from the server would fail too (no registry, no
memory, a lockfile that does not resolve), and it would fail with nowhere to
report it, on a page with no application to render the failure. It would also
inherit the server's memory band and be protected ahead of the user's chats and
agents. Nothing is spawned either way: the terminal app is supervised, always
running, and sits at a *lower* (more protected) memory band than this server,
so the page points at something that outlives it.

The terminal's origin label is minted per workspace, so the page cannot carry
it; the server reads it from the app registry (`data/.state/apps.toml`) at
render time and the page's own script derives the origin from the browser's
location, mirroring `system/libs/workspace_ui/src/origin.ts`. When there is no
terminal registered -- the terminal app starts alongside the other apps, not
before them -- the frame stays hidden and the prose stands alone.

The page returns to the interface on its own once a bundle exists, so a
rollback (or a build run in that terminal) needs no further action. It polls the
`X-Frontend-Built` header rather than reloading on a timer: a whole-page refresh
would destroy the terminal session every few seconds, right while it is being
typed into. The timer-based reload survives only inside `<noscript>`, where
there is no terminal to protect.

Two things make that state recoverable rather than terminal. Every app-shell
response carries an `X-Frontend-Built` header, so the placeholder is
distinguishable from the real app without pattern-matching its markup -- that is
what the apply's frontend probe reads. And `/assets/<path>` is registered
unconditionally rather than only when the bundle exists at startup: a route
decided at construction time can never notice a bundle that appears later, and
without it asset requests fall through to the SPA catch-all and come back as
`index.html` with a `text/html` type, which the browser refuses as a module
script -- a blank screen instead of the placeholder. A genuinely missing asset
gets a plain 404.

## When the served code is behind the tree

A missing bundle is the loud version of a more general problem: an update lands
by advancing the working tree, and this process only becomes consistent with it
once it restarts into the merged code. The apply does both in one motion, but
an interrupted apply, a failed apply whose rollback could not restore health,
or a hand merge outside the flow can leave a live server rendering old code
over new on-disk state -- silently, which is the shape the geebspace incident
took.

So the server says so. It records the tree HEAD it started from and, when the
live tree has moved *in a way that affects what this process runs*, injects a
`system-interface-update-staleness` meta tag into the built app shell, from
which the frontend renders one dismissible informational line. Three values, checked in
this order:

- `update-emergency` when the apply's emergency record
  (`data/.state/update-apply/emergency.json`) is present -- a rollback that
  could not put a healthy workspace back. It outranks the other two because it
  is the one state here that does not resolve itself, and the one neither of
  them can see: that exit clears the marker, and its rollback has already put
  the tree content back, so both would read as consistent.
- `update-interrupted` when the apply's marker
  (`data/.state/update-apply/marker.json`) is present.
- `updated-not-activated` otherwise.

"Affects what this process runs" is the whole design (see `update_staleness.py`
for the rules and their test table). A bare HEAD comparison would show the
banner near-permanently -- agents commit their ordinary work in this repo
constantly, the apply's own version-history commit lands after the restart, and
a frontend-only apply rebuilds the served bundle without restarting -- so the
check diffs the startup HEAD against the current one and reports only when a
changed path is backend code this process imports, a manifest its environment
was resolved from, or mngr. The banner
informs only; acting on it stays with the agent.

## The update notice

The careful flow's apply (`update_self.py apply --keep-rollback-point`, see
"Updating the running UI") does not discard its snapshots on success: it leaves
`data/.state/update-apply/last-good.json`, a record of the merge it landed, the
copies it kept, and the apps and supervisord programs included in rollback: each critical
app whose program or bundle the apply changed, and every other app and service of
the harden pass it landed (`--pass-scope`), with the programs that pass's scope
names for them. A frontend apply includes
both chat and shell (the critical bundle owners), even if only
one app's source changed, because one build replaces every bundle. The shell turns that
record into a notice only a person closes (`shell/update_notice.py`):
one top banner beside the staleness one, naming every app the record names (or
the workspace, when it names none), saying they were updated a moment ago and
offering "Roll back" and "Everything seems good". It is one banner rather than a
note on each touched app's windows because the record is one rollback point: a
rollback takes everything it names back together. The shell watches the file and
pushes every distinct reading over the socket as `update_notice_changed` (and
seeds it on connect), so the banner appears, shows a rollback's progress and
outcome, and goes away on every window without a reload.

Both verbs are the update-self script's own subcommands, run rather than
reimplemented (`POST /api/updates/pending/confirm` runs `confirm-last`, which
drops the record, and the copies with it when no rollback ran, since a failed
rollback keeps its copies for an agent; `POST /api/updates/pending/rollback` starts
`rollback-last`, which reverts the merge forward, restores the copies, restarts
only the recorded programs, and writes its progress and outcome back into the
record). The rollback is launched detached in its own session: it restarts the
shell's own program when the shell was touched, and supervisord stops that
program as a group, so a child of the shell would die halfway through its own
work. "Roll back" asks first, naming the update and what it restarts; a rollback
whose diff reached the workspace's own setup restores the files and its outcome
names the command an agent must run. A preview shell shows the notice but
refuses both verbs. `GET /api/updates/pending` is how the careful flow learns a
previous update is still unconfirmed.


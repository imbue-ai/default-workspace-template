# The Screen: a streamed Linux desktop as a workspace window

Status: designed 2026-10-17 (Josh + agent); implemented on `mngr/vnc-frame` (this repository and the mngr monorepo's branch of the same name) as of 2026-10-17, with the verified deviations recorded in the implementation notes of section 22.
Audience: the agent implementing `system/apps/screen`, the shell (`system/apps/system_interface`), `system/libs/workspace_layout`, `system/libs/app_manifest`, the share gateway (`system/services/share_gateway`), `system/services/oom_priority`, the env.d units, the `agentic-browser-fleet` and `manage-desktop` skills, and the deletion of `system/apps/browser`.
Builds on the desktop interface ([plan](../../blueprint/desktop-interface/plan-desktop-interface.md), [contracts](../../blueprint/desktop-interface/contracts.md), [concepts](../../blueprint/desktop-interface/concepts.md)), [window-bound-resources.md](../window-bound-resources.md), [stop-when-no-windows.md](../stop-when-no-windows.md), and three specs in the mngr monorepo: `specs/published-ports/spec.md`, `specs/turn-relay/spec.md`, and `specs/screen-studio-integration/spec.md` ("the integration spec" below).

## 1. Purpose and scope

A workspace gets one Linux desktop, the **Screen**: an X server with a window manager, streamed to the user's browser over WebRTC by [neko](https://github.com/m1k1o/neko) (v3, Apache-2.0), shown as one window of the workspace's desktop interface, and driven by the human or by an agent, one at a time.
Any program that needs a display runs on it, the workspace's browser included; the browser app that filmed a Chromium of its own goes away.

In scope: the Screen app (processes, packaging, the service, the viewer page), connectivity (ports, relays, candidates), the control lease, display sizing, visibility and cost policy, lifecycle and memory, clipboard and files, mobile, the three fullscreen layers, the share gateway's part, the browser's migration and deletion, the agent-facing command, tests, and the implementation order.
Out of scope, with the follow-ups listed in section 21: per-window capture, more than one Screen, HiDPI modes, a view-only toggle per visitor, and a damage gate for neko's encoder.

Prose names: "desktop" is the shell's concept (a named collection of windows); the thing this spec adds is always "the Screen".

## 2. Background

### 2.1 Today's browser app

`system/apps/browser` runs one headful Fortress Chromium per browser on a private Xvfb (`:99`, `system/supervisord.conf.d/xvfb.conf`), filmed by pixelflux as damage-driven H.264 stripes over one WebSocket with pcmflux audio, XTEST input, XFixes clipboard, a per-viewer capture region, a window guardian that keeps one toplevel per display, and a Flask daemon (`browser-service`, port 8081) that owns the fleet, the ownership lease, the CDP proxy (port 8083) agents drive through, the profile manifest, and the OOM re-tagging of Chromium's renderers.
Its lifetime rule is [window-bound-resources.md](../window-bound-resources.md) section 4.4: Chromium stops once no window shows it.

### 2.2 Measurements that decided the engine

From the prototype lab (2026-10-16; the raw results are outside this repository, in the handoff material):

- Clean path, same region: our stack 41 ms click-to-photon at p50, neko 73 to 84 ms; from a user's machine both "fairly responsive", ours pixelated in motion.
- 100 ms, 1 % loss: neko over UDP 205 ms and usable, over TCP 307 ms; under a 2 Mbps cap our stack collapsed and neko "could almost stream video".
- gVisor: `neko serve` costs about 37 points of one core with a viewer attached whatever the picture does, x264 at 1080p 20 fps about 33 under runc and 42 under gVisor; nothing with no viewer.
- Memory: neko, Xorg, XFCE and PulseAudio with no apps and no viewer 213 MB under gVisor; the browser's current display and stream pieces 150 to 200 MB; Chromium about 1 GB either way.

Neko was chosen for native WebRTC playout (jitter buffer, hardware decode, NACK), usability under loss, steady quality, audio and clipboard, a host model, a screen-size API, and maintenance; it is what Dots (OpenAI's agent desktop) runs.
Neko is configured, not forked.

### 2.3 Facts about neko this design relies on (verified 2026-10-17)

- The video pipeline starts with the first session that requests video and stops when the last one leaves; nothing runs with no viewer.
- Input from a session that is not the host is refused by `controlRequest`; with `implicit_hosting` off and a host present, the host is notified and the input is dropped; `locked_controls` lets only admins host, and turning it on clears a non-admin host.
- The API-token session (`session.api_token`) is admin but is not in the sessions map and has `can_connect = false`, so it can never be host; it can call every admin REST route: `POST /api/room/settings`, `POST /api/room/control/take`, `/give/{id}`, `/reset`, `POST /api/room/screen`, `GET /api/room/screen/shot.jpg`.
- `SetScreenSize` only selects a size the X server already advertises; it creates no mode.
- WebSocket events are `system/init`, `signal/request|offer|answer|provide|candidate|video|audio|close|restart`, `control/host|request|release|move|scroll|buttonpress|buttondown|buttonup|keypress|keydown|keyup|touchbegin|touchupdate|touchend|cut|copy|paste|select_all`, `screen/updated|set`, `clipboard/updated|set`, `keyboard/modifiers|map`, `session/created|deleted|profile|state|cursors`, `send/unicast|broadcast`, and `client/heartbeat`.
- A session authenticates by cookie, by `Authorization: Bearer`, or by `?token=`; the `multiuser` member provider admits any display name with the user or the admin password.
- `screen/set` over the WebSocket, and the REST screen change and screenshot, are admin-only.

### 2.4 Facts about the shell and Studio

- A page learns `shell:shown` and `shell:hidden` only when its window is minimized, its desktop is switched, or it is pulled out; there is no occlusion signal, and the browser viewer derives its own visibility from `document.hidden` and an IntersectionObserver.
- The shell's page iframes carry `allow="clipboard-read; clipboard-write"` and no `allow-pointer-lock`; `navigator.keyboard.lock()` works only from the Studio page (the integration spec, section 2).
- Bands are set once at launch; only chat agents are re-banded at runtime (`system/services/oom_priority/README.md`).
- A manifest declares `critical`, `stop_when_no_windows`, `priority`, `system`, `launch_paths`, `window_closed_path`, `message_handlers` and `[wiring] programs` (contracts section 2).

## 3. Decisions

Recorded so the implementation need not re-argue them.

| # | Decision |
|---|---|
| D1 | One Screen per workspace, `1920x1080` at most, every program that needs a display runs on it. No per-window capture, no second display. |
| D2 | The Screen is an app, `screen`, display name "Screen", `system = true`, not critical, `stop_when_no_windows = false`; its one page is `/`. The browser is a launch path of it, not an app. |
| D3 | The stack never stops for emptiness. It starts on first demand and ends only under memory pressure or by Quit, and the whole group ends when any member does. |
| D4 | One controller at a time, human or agent. Any human with a grant may take control, "Take control" always wins, agents never preempt a human and queue behind each other, the owner can never be locked out. |
| D5 | Neko's `locked_controls` is the agent lease: locked while an agent drives, unlocked while a human may. Human sessions are non-admin neko members; the Screen service is the only admin. |
| D6 | Agents inject input with xdotool on the display and use neko only for screenshots and the lease. |
| D7 | The controller's window drives the Screen size; other viewers letterbox; a phone never resizes it. Sizes are quantized to 16 px, debounced, and capped at 1080p. |
| D8 | Three video profiles, `idle` 5 fps, `watch` 20 fps, `control` 30 fps, chosen by the service from aggregate state and selected by every session alike. |
| D9 | Audio is off per session until the viewer unmutes. |
| D10 | A viewer closes its media when its window is hidden; neko counts, so the encoder stops with the last one. |
| D11 | Connectivity is ICE over the workspace's published port (UDP and TCP mux on 59000), the share relay's `tcp` proxy, and client-only TURN; the viewer rewrites the server's candidate to the endpoints it was told; neko needs no NAT configuration. |
| D12 | The viewer connects to neko through the Screen service's origin: the service proxies neko's HTTP and WebSocket under `/neko/`, so there is one registered origin and the doors' identity header reaches the service on every request. |
| D13 | `FULLSCREEN` is a fifth placement state beside `MAXIMIZED`; immersive mode is the chrome's and is never persisted; fullscreen inside the X session is the window manager's. The three are orthogonal. |
| D14 | Clipboard text and HTML flow through neko; images flow remote-to-local through neko's clipboard image route and local-to-remote through the service's own X clipboard module. |
| D15 | The Fortress browser moves onto the Screen: one Chromium, one profile, as many windows as the user opens, the window manager managing them; the fleet API, the CDP proxy and the profile persistence move into the Screen service; pixelflux, pcmflux, XTEST, the guardian and the per-viewer capture go. |
| D16 | The display is `:99`, taken from the first commit; `xvfb.conf` is deleted in the same change. |
| D17 | Nothing measured inside the workspace is trusted for enforcement; relay quotas are the connector's (the TURN spec). |

Decision 17 of [concepts.md](../../blueprint/desktop-interface/concepts.md) and section 4.4 of [window-bound-resources.md](../window-bound-resources.md) (the fleet holds one browser; closing its last window stops it) are superseded by D3 and D15 for the browser: closing the last Screen window stops nothing.

## 4. What the user sees

### 4.1 Opening the Screen

The launcher lists two rows for the app: "Screen" (the `open` launch path) and "Browser" (the `browser` launch path, taking an optional start page).
Both run with the `focus` target, so a launch opens the one Screen window or focuses the one that exists on the desktop and never opens a second; "Browser" additionally starts Chromium if it is not running and brings it to the front, opening the start page as a new tab when given.
A second window on the Screen is still possible by an explicit open with `if_present: new` (`workspace-layout open screen --if-present new`), and is one more viewer of the same Screen.
The first open starts the stack; the window shows "Starting the screen" until the first frame.

The window's title follows the frontmost X window's title, polled by the service, so the taskbar reads "Firefox", "Browser" (for Fortress) or "Screen" for an empty desktop.

### 4.2 Watching and control

The viewer shows the live Screen.
A strip at the top names who has control: "You", "<name>", "Agent <name>", or "Nobody".
When someone else has control, the strip offers "Take control"; input from a non-controlling viewer is not sent.
When an agent has control, the Screen is dimmed slightly, the agent's name is shown, and "Take control" is there, as in the browser app's agent overlay; a URL the human asked to open while an agent drives is held and offered as it is today.
"Give control back" appears for the human who took it from an agent, so the agent can resume (the browser's `return_to_agents`).

Several humans may watch at once, each from their own client; the controller's cursor is the remote one, and other viewers' cursors are shown as neko's inactive cursors.

### 4.3 Audio, clipboard, files

A large mute control in the strip, off by default, toggles audio for this viewer.
Copy and paste work across the boundary while this viewer holds control: text and HTML both ways, images both ways (section 13).
Dropping a file onto the viewer saves it in the Screen's desktop folder through the service's own `POST /api/drop`: the XDG desktop directory (`XDG_DESKTOP_DIR` in `~/.config/user-dirs.dirs`, else `~/Desktop`; `~/Desktop` in the workspace image), the file's name kept and a clash given a numeric suffix; the service then opens the folder in a file manager window on the Screen and the strip says where it landed; a file chooser opened on the Screen is left to the Screen.
The desktop shows no icons (`xfdesktop`'s icon style is set to none before each stack start): an icon wants a double click, which a phone cannot give, and anything an icon would do belongs in the panel.

### 4.4 Window states

Besides the four placement states, the window menu and the title bar offer "Fullscreen", which fills the shell's viewport, hides the taskbar and the title bar, and shows a strip on hover at the top edge with "Exit fullscreen" and "Immersive".
"Immersive" (also in the window menu) asks the Studio chrome to take the whole app window with the keyboard locked (section 12); it is hidden when the chrome cannot do it.

### 4.5 Mobile

On a phone the Screen stays at its size; the viewer pans and pinch-zooms the video, a tap is a click, a long press is a right click, a two-finger drag scrolls, a modifier bar offers Ctrl, Alt, Shift, Esc and Tab, and a keyboard button raises the soft keyboard (section 14).

### 4.6 Visitors

A visitor admitted by a workspace grant or by a `screen` app grant sees the same viewer and may take control (D4).
Over the share, the stream reaches them through the relay or through TURN (section 5).

### 4.7 When the stack is gone

If memory pressure sheds the stack, or the user quits the app, the window shows "The screen was stopped" with the reason and a "Start" button, and every program that ran on it is gone with the X server.
The next open starts a fresh stack; Chromium comes back with its profile and tabs (section 16).

## 5. Connectivity

### 5.1 Ports and processes

| Thing | Where | Notes |
|---|---|---|
| Screen service | `127.0.0.1:8090`, registered as the app's URL | HTTP and WebSocket, the viewer's only origin |
| neko HTTP and WebSocket | `127.0.0.1:8091` | reached only through the service's `/neko/` proxy |
| neko UDP mux and TCP mux | `0.0.0.0:59000/udp` and `0.0.0.0:59000/tcp` inside the container | the workspace's two published ports, declared by every create template as `publish__extend = ["59000/udp", "59000/tcp"]` (the `modal` template declares `["59000/tcp"]` alone: Modal tunnels carry TCP only, and mngr refuses a UDP declaration before the sandbox exists) |
| CDP proxy | `127.0.0.1:8083` | unchanged from the browser app |

### 5.2 `GET /api/connect-info`

The viewer's one question: `{"ice_servers": [...], "candidates": [{"host", "port", "protocol"}], "expires_at"}`.
The service asks, with a two-second bound each and in parallel, the desktop (`$LATCHKEY_GATEWAY/minds-api-proxy/api/v1/agents/<id>/media-endpoints` and `.../ice-servers`, the integration spec section 4, reached as the chat app's notification client reaches the desktop) and the share gateway's loopback route (`GET http://127.0.0.1:8791/connect-info`, section 15), and answers the union, with candidates filtered to guest port 59000 and de-duplicated.
`?relay=1` is the only time the service asks for ICE servers; without it `ice_servers` is empty (the TURN spec's direct-first rule).
Both brokers may be absent; the answer is then an empty candidate list, which the viewer treats as "try the server's own candidate as given".
The answer is held in memory only.

### 5.3 Signaling and candidate rewriting

The viewer opens `wss://<origin>/neko/api/ws?token=<session token>` (section 6.3 says how it gets the token), sends `signal/request` with the video id the service last announced and `audio.disabled: true`, receives `signal/provide` with the offer and neko's ICE servers (none), answers, and exchanges candidates.
For every server candidate it receives, the viewer emits one rewritten copy per entry of `candidates` whose protocol matches, replacing the address and port, giving each copy a distinct foundation so the browser treats them as distinct candidates, and keeping everything else; it drops the original unless `candidates` is empty.
Its own peer connection carries `ice_servers` from `connect-info` and `iceTransportPolicy: "all"`.
A connection that does not reach `connected` within four seconds, or that fails later, refetches `connect-info?relay=1` and restarts ICE; the viewer records `needs-relay` per client in local storage and fetches with `relay=1` first next time.

### 5.4 Fallback without WebRTC

A client that cannot establish any pair (no candidate reachable, no relay) shows the Screen through neko's JPEG screencast (`capture.screencast` on at 5 fps, quality 60, proxied at `/neko/api/room/screen/cast.jpg`) with input still over the WebSocket, says so loudly in the strip, and keeps trying WebRTC (15 s after the fall, then 30 s, then every 60 s, and at once on the strip's "Retry now"), each time with fresh `connect-info?relay=1`; the screencast shows until a retry's peer connects.

## 6. Control

### 6.1 The lease

The service owns a `ScreenControl` state machine with the browser app's shape: `controller` (`human`, `agent`, `nobody`), `owner_agent_id`, `human_pinned`, a FIFO wait queue and a resume queue for agents, a sticky agent lease with a 60-second idle TTL, and a 12-second claim window for a granted lease.
The verbs are the browser's: `acquire(agent, reclaim, wait)`, `release(agent)`, `take_control(session)`, `handoff(agent, reason)`, `return_to_agents(session)`.
Rules (D4): any human session may `take_control`, which preempts an agent (queued to resume first) or another human; `reclaim` by an agent is allowed only as the skill allows it today (an explicit user instruction); an agent's `acquire` while a human is pinned answers `busy_human`.

### 6.2 Projection onto neko

| Lease state | neko settings and host |
|---|---|
| agent holds the lease | `locked_controls: true` (admin-only hosting); the human host, if any, is cleared by neko; human input is refused server-side |
| a human holds control | `locked_controls: false`; that human's session is made host with `control/give/<session>` from the admin API token |
| nobody | `locked_controls: false`, no host; the first human input is a `control/request` that neko grants because there is no host, and the service learns it from `control/host` and records that human as controller |

`session.implicit_hosting` stays off: with it on, any human's first key would take host from another human, and taking control must be explicit (D4).
With it off, a request from a non-host while a host exists is refused and the host is notified, which the viewer never provokes since it sends input only while it is the controller.
The service subscribes to neko's `control/host` and `session/*` events on an admin WebSocket session of its own and keeps its state in step: a host change it did not make is a human taking control of an uncontrolled Screen.
`session.control_protection` stays off, `session.private_mode` off, `session.inactive_cursors` on, `session.merciful_reconnect` on.

### 6.3 Sessions and identity

Neko runs the `multiuser` member provider with a user password and an admin password the service generates at each stack start and holds in memory and in neko's config file (mode 600, under `data/.state/screen/`).
Humans never see them: the viewer calls `POST /api/session` on the service, which reads `X-Imbue-Identity`, logs the human in to neko with the user password and a display name from the identity (the owner's name or email local part, "Guest" for an identity without one), and answers the neko session token the viewer then uses for `/neko/...`.
The viewer logs its session out (`POST /neko/api/logout`) on `pagehide`, and neko drops a `multiuser` session on logout, so sessions do not accumulate across page loads; the service also deletes any session that has been disconnected for ten minutes.
The service's own admin session and REST calls use `session.api_token`, generated the same way.
A request with no identity header is the owner: the gateway and the forwarder both stamp one, so a header-less request can only have come over loopback, from an agent or a test.

### 6.4 Agents

An agent drives the Screen with `DISPLAY=:99` and xdotool, or Chromium through the CDP proxy, after `screen control acquire` (section 17); the lease decides whether a human's input is refused meanwhile, not whether the agent's X input lands (X does not know about leases), so the skill's rule stands: an agent that lost the lease stops and ends its turn.
Screenshots come from `GET /api/screenshot`, which the service answers from neko's admin screenshot route (`/api/room/screen/shot.jpg`) with the requested quality.

## 7. Display and sizing

### 7.1 The X server

Xorg with the dummy driver on `:99`, `xorg.conf` rendered by the service at stack start: `VideoRam` sized for the 1080p cap at 32 bpp with headroom, `Virtual 1920 1080`, a `Modes` list seeded with `1920x1080`, `1600x900`, `1280x720`, neko's `xf86-input-neko` touchscreen as the one configured input device (`Driver "neko"`, its socket under the stack's runtime dir, the layout's `CorePointer`; mouse and keyboard arrive over XTEST), and `-noreset -nolisten tcp`.
XFCE as neko's image ships it: `startxfce4` with the panel, Thunar and xfwm4 compositing on, PulseAudio with a null sink as the default device.
The session runs as the workspace user with `HOME` the workspace home, so XFCE and application state ride the persistent volume and the backup.

### 7.2 Sizing

- The controller's viewer reports its stage size in CSS pixels on resize, debounced 300 ms, to `POST /api/screen-size`, and its latest size as soon as its session controls with the stack running; a non-controlling viewer's report is ignored; a phone's is never sent (D7).
  A phone is a phone-sized viewport with a finger as the primary pointer (the shared `PHONE_MEDIA_QUERY` and `TOUCH_MEDIA_QUERY` both); a desktop window that is merely small is not one, and the Screen resizes to fit it.
- The service rounds width and height down to multiples of 16, clamps to `[640x480, 1920x1080]`, and if the size differs from the current one: adds a mode named `<w>x<h>_60` with `xrandr --newmode` (a `cvt`-computed modeline) and `--addmode` on the dummy output when it is not already there, then `POST /neko/api/room/screen` with it.
  Neko restarts the capture and broadcasts `screen/updated`; viewers letterbox the new aspect.
- Added modes are kept for the stack's life; they cost nothing.
- Every viewer draws the video at its intrinsic aspect, centred, letterboxed in the stage.

## 8. Video profiles, visibility and audio

### 8.1 Profiles

Three pipelines in neko's config, all H.264 constrained baseline from x264 with `tune=zerolatency`, `byte-stream=true`, keyframe interval one second:

| id | fps | bitrate | preset | threads |
|---|---|---|---|---|
| `idle` | 5 | 800 kbps | ultrafast | 1 |
| `watch` | 20 | 2500 kbps | veryfast | 4 |
| `control` | 30 | 4000 kbps | veryfast | 4 |

The service chooses one profile for everyone from aggregate state: `control` while a human holds control and has sent input in the last 10 seconds, `idle` while nobody has sent input for 60 seconds and no agent holds the lease, `watch` otherwise.
It announces the choice on its own WebSocket (section 10.2), and every viewer selects it with `signal/video`, so neko runs one encoder.
`show_pointer` is off per pipeline; the controller's pointer takes the remote cursor's shape from the image neko sends over the peer's data channel (section 12).

### 8.2 Visibility

A viewer is visible when its document is not hidden, the shell said `shown` (or never said `hidden`), and its stage intersects the viewport (an IntersectionObserver); it re-evaluates on `visibilitychange`, on the shell's messages, on intersection changes, and every 1.5 seconds.
Hidden for 5 seconds: `signal/close` (the WebSocket stays, the lease is untouched).
Visible again: `signal/request`.
Neko stops the encoder when the last session has closed its media, which is D10's zero-cost state.
A viewer that holds control and goes hidden keeps the lease; its input is only sent while visible.

### 8.3 Audio

Every session requests video with `audio.disabled: true`; the mute control sends `signal/audio` with `disabled: false` for this session alone.
Neko starts the audio pipeline when the first session enables audio and stops it when the last disables it; the implementation verifies that in the image and, if the pipeline runs regardless, points `capture.audio.device` at a null sink until any session enables it.

## 9. Lifecycle and memory

### 9.1 Processes

A supervisord group `screen` of four programs, each tagged `oom_tag_service.py screen`, all `autostart = false`, `autorestart = false`:

| program | command |
|---|---|
| `screen-xorg` | `Xorg :99 -config data/.state/screen/xorg.conf -noreset -nolisten tcp` |
| `screen-session` | `startxfce4` with `DISPLAY=:99` |
| `screen-pulse` | `pulseaudio --daemonize=no --exit-idle-time=-1` with the null sink loaded |
| `screen-neko` | `neko serve --config data/.state/screen/neko.yaml` |

The service itself, `screen`, is the manifest's program: `autostart = true`, `autorestart = true`, tagged `screen`, `stopasgroup = false`, and it is what the shell parks and wakes.
The manifest's `[wiring] programs` names the four.

### 9.2 Start on demand

The service starts the group when it has to: a page load of the viewer (`GET /`), the stopped overlay's Start button, the `browser` launch path, `screen run`, or an agent's `acquire`.
It renders `xorg.conf` and `neko.yaml`, starts `screen-xorg`, waits for the display socket and the input driver's socket (neko dials the driver once at its start and exits without it), then `screen-pulse`, `screen-session`, and `screen-neko`, and answers `starting` until neko's health route answers.
Programs the user or an agent start on the Screen are launched with `DISPLAY=:99` and tagged `AGENT_SUBPROCESS` by `screen run`, which is the band above the group's (section 9.4).

### 9.3 The group ends together

A supervisord event listener, `screen-watch` (the shape of `oom-tag-backstop`), subscribes to `PROCESS_STATE_EXITED` and `PROCESS_STATE_FATAL` for the group's programs; when any member leaves `RUNNING`, it stops the other members, records a `screen_stopped` event with the member and reason in `data/.state/screen/events/screen/events.jsonl`, and the service broadcasts `stopped` to every viewer with that reason (section 4.7).
Quit (`POST /api/apps/screen/quit`, the window menu) stops the group through the service first, then the service; the shell's stop and start routes act on the service alone and the group follows the service's own start-on-demand.
Before the service stops the group for any reason it can see coming (Quit, its own shutdown), it checkpoints Chromium's tabs while Chromium can still be asked, the reason the browser program ran with `stopasgroup = false` today.
Logging out from XFCE's own menu ends `screen-session`, which the watcher treats as the group ending; the session's log-out item is hidden by the rendered XFCE configuration so the window menu's Quit is the one way out.
Nothing stops the group for lack of viewers, windows, or apps (D3).

### 9.4 Bands

`SERVICE_BANDS` gains `screen = 72`, between `system_interface` and `files` (the `browser` and `xvfb` bands go with the browser app), for the service and the four group members alike.
Chromium's processes are re-tagged into the shared-browser band by the migrated `oom_retag` sweep as today, and `screen run` launches land in `AGENT_SUBPROCESS` (900).
Programs started from the Screen itself (the XFCE menu, a terminal on it) inherit the session's band, so the service runs a raise-only sweep every 10 seconds, the shape of the Chromium retag: every descendant of `screen-session` whose executable is not one of the session's own (`xfce4-session`, `xfwm4`, `xfce4-panel`, `xfdesktop`, `xfsettingsd`, `xfconfd`, and the D-Bus daemon they start) is raised to `AGENT_SUBPROCESS`, and Chromium's to the shared-browser band as today.
Under pressure the order is therefore: programs on the Screen, then Chromium's renderers, then the stack.
The manifest's `priority = "screen"`; `system/test_app_manifests.py` and the band ordering tests pin it.

## 10. The service

### 10.1 Routes

All on `127.0.0.1:8090`, identity from `X-Imbue-Identity`:

| Route | Purpose |
|---|---|
| `GET /` | the viewer page |
| `GET /_static/app_contract.js`, `/_static/context_menu.js` | the shell's modules, served from this origin as every app does |
| `POST /api/session` | a neko session token for this identity (section 6.3) |
| `GET /api/connect-info` | section 5.2 |
| `POST /api/screen-size` | section 7.2 |
| `GET /api/state` | lifecycle, lease, profile, frontmost window title, Chromium state |
| `GET /api/ws` | the service's own events to viewers (section 10.2) |
| `POST /api/control/take`, `/return` | the human verbs for this identity's session |
| `POST /api/control/acquire`, `/release`, `/handoff` | the agent verbs, with `X-Mngr-Agent-Id` as the fleet API has them |
| `GET /api/screenshot` | section 6.4 |
| `POST /api/run` | `screen run` (section 17) |
| `GET /api/windows`, `POST /api/windows/<id>/activate` | the X window list and raise, through `wmctrl` |
| `POST /api/clipboard/image` | local-to-remote image (section 13), multipart `session_id` and `image`, the session in control alone |
| `POST /api/drop` | files dropped on the viewer (multipart `session_id` and `files`, at most 64 MiB together), written into the desktop folder (section 4.3) |
| `POST /launch/browser` | the `browser` launch path: ensure Chromium, open the start page, answer `{"path": "/"}` |
| `POST /api/open-url` | the `open:url` handler: a tab in Chromium, raised, with the held-URL rule while an agent drives |
| `/browsers/...`, `GET /json/version` and the CDP proxy | the migrated fleet API (section 16); `POST /browsers/<name>/pending-url/cancel` takes `session_id` in its body or query and answers the session in control or the owner, with or without a session |
| `/neko/...` | the reverse proxy to neko, HTTP and WebSocket, with the service's own rate limit on `/neko/api/login` |

A visitor (an identity whose `owner` is false, section 4.6) may call only the viewer's routes, where the lease arbitrates: the page, `/api/state`, `/api/start`, `/api/connect-info`, `/api/ws`, `/api/session`, the human verbs, `/api/input-seen`, `/api/screen-size`, `/api/clipboard/image`, `/api/drop` and `/browsers/<name>/pending-url/cancel` with a visitor's session id (the owner's session, whose id the state document carries while it controls, answers a visitor 403; a paste and a drop need the session in control, and a cancel the session in control or the owner), and `/neko/...`.
Every other route, `/api/run`, the agent verbs, the screenshot, the window tools, a session-less `/api/screen-size`, `/launch/browser`, `/api/open-url` and the rest of the `/browsers` API, answers a visitor 403; a request with no header is the owner (section 6.3), so agents and the shell, which reach the service over loopback, are not affected.

### 10.2 Events to viewers

`/api/ws` sends `state` (the `GET /api/state` document) on connect and on every change, `profile` when the chosen video id changes, `stopped` with a reason, `clipboard` when the remote clipboard holds an image (so the viewer can fetch it), and `handoff_request` as the browser app sends it.

### 10.3 Files

`data/.state/screen/`: `xorg.conf`, `neko.yaml` (mode 600), `control.json` (the lease, checkpointed so a service restart keeps it), `modes.json` (the sizes added this boot), `events/screen/events.jsonl`.
Chromium profiles stay under `$MNGR_HOST_DIR/browser-profiles/` and the tab manifest under `data/.apps/browser/instances.json`, as today, renamed in a later cleanup.

## 11. The viewer

A page in the `workspace_ui` npm workspace, built into the service's static output: the video element, the strip, the overlays (starting, stopped, agent has control, screencast fallback), the mobile controls, and no in-page chat.
It imports the app contract and connects with `capabilities: {navigation: false, closeChord: true}`; its location report is `/` with the title from `state`; `onCloseRequest` sends Ctrl+W to the remote as a key chord rather than closing the window.
It speaks neko's events directly (section 2.3) for signaling, input, cursors, clipboard and screen updates, and the service's `/api/ws` for everything else.
Keys are mapped from `KeyboardEvent.code` to X keysyms with neko's own table, copied under its Apache-2.0 notice into `system/libs/workspace_ui`.
Input is sent only while this viewer's session is the controller and the viewer is visible.
Neko's constrained-baseline H.264 decodes natively over WebRTC in every browser Studio supports, so the viewer needs no WebCodecs and nothing of the browser app's decoding path.

## 12. The three fullscreen layers

| Layer | What it is | Enter | Leave |
|---|---|---|---|
| L1, immersive | the Studio window or browser tab in Fullscreen-API fullscreen with Keyboard Lock (the integration spec, section 5) | "Immersive" in the hover strip or the window menu (a user gesture) | hold Escape (Chrome's exit), or the strip's control |
| L2, `FULLSCREEN` | a placement state: the window fills the shell viewport over the taskbar, no title bar, hover strip at the top edge | title-bar control, window menu, `workspace-layout place --state FULLSCREEN` | the strip's "Exit fullscreen", the window menu, a layout op |
| L3 | fullscreen inside the X session (an app's F11, xfwm4's own key) | the app or the window manager | the app or the window manager |

They are orthogonal (D13): each enter and leave changes only its own layer.
L1 is never persisted; L2 is persisted per client like every placement state; L3 is the X session's.

Keys: a layer never changes which keys reach the viewer except L1, which with the lock delivers every key but the held Escape.
Without L1 the browser's and Electron's reserved chords never reach the page, which the strip says when the user is in L2 ("Press Immersive to send every key").
The viewer forwards every key it receives to the remote while the human holds control, Ctrl+W included.
On macOS the remote window manager's Alt+Tab works without L1 (Option+Tab is not reserved); on Windows it needs L1; on a Linux host the host's window manager keeps Alt+Tab, and the Screen's windows are switched with the mouse or its taskbar.

Shell changes for L2: `WindowState.FULLSCREEN` in `workspace_layout.primitives` and `records.ts`; `PLACEABLE_STATES` and the `place` verb's `--state fullscreen`; `frame_for_state` and `frameForState` answer the backdrop plus the taskbar's band (`{0, 0, 1, 1}` over a backdrop that excludes the taskbar is not enough, so the fullscreen frame is expressed as "the viewport" in both editors and the geometry vectors gain the case); the title bar's state control cycles to it from `MAXIMIZED`'s "restore"; `WindowMenu` gains the row; `Window.ts` draws no title bar and the hover strip in this state; `toggleMaximized` is unchanged; un-snap by drag does not apply (there is no title bar to drag); the window keeps its frame for restore like every state.
`livePages.ts` adds `fullscreen` to the iframes' `allow` list and `allow-pointer-lock` to their sandbox list.

The pointer: no cursor is in the video (`show_pointer` is off, section 8.1).
neko sends the X cursor's image (width, height, hotspot and a PNG) to every peer over the WebRTC data channel when the channel opens and whenever the cursor changes, and its position to the peers that are not the host; neither is a WebSocket event.
The viewer whose session controls (or may take control, with nobody holding it) gives the stage's CSS `cursor` that image with its hotspot (`url(<data URI>) <x> <y>, auto`), shrunk with the drawn video when the stage renders the desktop smaller than 1:1 and capped at 128 px a side, where browsers stop honouring a cursor image; an image the browser will not draw leaves its own pointer.
Every other viewer keeps the default pointer; the position and neko's inactive cursors (`session/cursors`) are not drawn. No picture has a pointer painted into it, the screencast included (neko's global `capture.video.show_pointer`, which the screencast pipeline takes, is off), so in the screencast the browser's own pointer is the one pointer.

Pointer lock: a viewer control, available while the human holds control, requests pointer lock on the stage for relative-motion programs; Escape releases it, and with L1's lock the held Escape releases both.
Neko's relative-motion support is verified in the implementation; without it the control is hidden.

## 13. Clipboard

- Remote to local: on `clipboard/updated` the viewer reads text and HTML from the event and writes them with the Clipboard API; when the service's `clipboard` event says an image is present, the viewer fetches `/neko/api/room/clipboard/image.png` and writes it.
- Local to remote: on focus and on paste, while this viewer holds control, the viewer reads the local clipboard; text and HTML go through `clipboard/set`; an image goes to `POST /api/clipboard/image` as this session (multipart `session_id` and `image`), which the service accepts from the session in control alone and places on the X clipboard with the migrated `xclipboard` module.
- The keyboard map: the viewer sends `keyboard/map` once per session from `navigator.language` and the layout it detects, defaulting to `us`.

## 14. Mobile

Under the phone layout (a phone-sized viewport whose primary pointer is a finger, section 7.2; a small desktop window is not one) the viewer keeps the Screen size and offers: a one-finger drag as a touch held and moved on the Screen (`control/touchbegin`, `touchupdate`, `touchend`), so a page scrolls under the finger as on any phone, and as a pan of the view once the video is pinch-zoomed (a drag that starts in the letterbox band around the video begins its touch where the finger first reaches the video); pinch-zoom of the video; tap as a touch at the touch point mapped into Screen coordinates (`control/touchbegin` and `control/touchend` with the finger's pressure, through neko's `xf86-input-neko` driver, so the Screen sees a touchscreen tap); long press as a right click; two-finger drag as scroll; a modifier bar (Ctrl, Alt, Shift, Esc, Tab, each latching for the next key, and Scroll Up and Scroll Down, three wheel steps each, for programs that ignore touch); a keyboard button focusing a hidden input whose `input` and `keydown` events become neko key events; and the mute and control controls of the strip.
Neko's touch events carry every direct interaction with the Screen (the tap and the drag); the zoom and the zoomed pan are the viewer's own, and the long press stays a right click because X touch has no second button and a context menu is what the press is for; a neko whose `system/init` says `touch_events` is false gets the tap as a click and the drag as a pan.

## 15. The share gateway

While the share stack is up:

- It fetches `GET /turn/ice-servers` with the relay token at start and at half life (the TURN spec, section 7).
- It renders the `screen-media` `tcp` proxy in every relay's frpc config, `localPort` 59000, `remotePort` the assignment's `media_port` (the integration spec, section 6).
- It serves `GET /connect-info` on its loopback port to the Screen service: `ice_servers` as fetched, and `candidates` as the assignment's `published_endpoints` (the box's own port, for a cloud workspace) plus one TCP candidate per relay address at `media_port`.
- A per-app grant on `screen` admits a visitor to the viewer origin as any app grant does; `system = true` changes nothing (contracts section 2).

## 16. The browser's migration

### 16.1 What moves into the Screen service

- The fleet API and its routes (`/browsers/...`, `POST /new` becomes `POST /launch/browser`), the ownership lease (now `ScreenControl`), the CDP proxy and its block list, the profile directory, the tab manifest and its checkpointing, the restore on start, `oom_retag`, `chrome_args`, `chrome_launcher`, `cdp_client`, `xclipboard`, the `open_popups_as_tabs` extension, the held-URL rule, the close-tab verb, and the agent wake-up messages.
- "The browser" is one Chromium with one profile, started by the `browser` launch path, `screen run`, or an agent's `new`; it is not stopped when its windows close (the user closes it from inside, or Quit ends the stack); its tabs are checkpointed as today and restored when it is next started.
- Chromium's windows are ordinary X windows: the user may open as many as they like, the window manager places them, and nothing pins or closes them.
  `Browser.setWindowBounds` leaves the CDP block list; `Emulation.setDeviceMetricsOverride` stays blocked.
- Fortress stays at v151 in the new env.d unit, x64 and arm64 pins alike. v153 (Fortress v3) was tried twice and dropped: it moves to a source-available license that charges organizations of our size for production use, and its engine-level recorded-human mouse motion makes a CDP mouse click hang under Playwright (1.58 and 1.64, with and without the launcher), which would stall agents driving the browser fleet as much as the tests. An upgrade waits on a release whose input path can be driven over CDP, and on a license decision.

### 16.2 What goes

`videopipe.py`, `audiopipe.py`, `mediastream.py`, `stream_conductor.py`, `xinput.py`, `window_guardian.py`, `telemetry.py`, `telemetry_watch.py`, `loop_bridge.py` where the service's own loop replaces it, the pixelflux and pcmflux dependencies, `assets/index.html` and its first-visit dialog, `system/supervisord.conf.d/xvfb.conf` and `browser.conf`, `system/apps/browser` as a directory, the `browser` and `xvfb` keys of `SERVICE_BANDS`, and the browser's rows in `system/test_app_manifests.py` and `docs/system/blueprint/desktop-interface/contracts.md`.
The viewer-sizing, capture-backend and videopipe tests go with them; the lease, CDP proxy, names, manifest and retag tests move.

### 16.3 Names and the skill

Browsers keep their names (`browser-1`), since the profile and the manifest are keyed by them, and the fleet still holds one.
`agentic-browser-fleet` keeps its verbs and exit codes against the Screen service; its skill text changes where the window-bound rule was: the browser no longer stops when its window closes, `new` opens the Screen window minimized as before, and `handoff` shows it.
The skill says that the browser shares the Screen with everything else the user runs there, so an agent raises its window (`screen activate`) before relying on a screenshot.

## 17. The agent-facing command and skills

`uv run --no-sync screen`, a console script of the Screen package, thin over the service like the fleet CLI:

| verb | does |
|---|---|
| `run <command...>` | starts the command on the Screen with `DISPLAY=:99`, tagged, detached; prints the pid |
| `windows` | lists X windows: id, title, class, geometry |
| `activate <id>` | raises a window |
| `shot [--quality N] [--out PATH]` | a screenshot, PNG by default |
| `size [WxH]` | reads or sets the Screen size (sets as the controller's viewer would, subject to section 7.2) |
| `control acquire\|release\|handoff "<reason>"` | the lease verbs, the fleet CLI's exit codes |
| `open` | opens the Screen window for the user through the shell's `open` op, minimized unless `--show` |
| `browser open [URL]` | opens the browser on the Screen through the launch path (section 16), URL as a new tab in front; what the XFCE session's Web Browser launcher, an `http(s)` link on the Screen and `sensible-browser` run through `screen-browser-open` |

The `manage-desktop` skill documents the Screen window and the `FULLSCREEN` state; a new `use-screen` skill documents the command, the lease rules (the human always wins), and that xdotool and the CDP proxy are how input is sent.

## 18. Packaging

A new env.d unit, `2000-screen.sh`, with a fast satisfied check and no marker file, installs from the pinned apt snapshot: `xserver-xorg-core` (which carries `cvt`), `xserver-xorg-video-dummy`, `xfce4` (the metapackage without the goodies), `pulseaudio`, `gstreamer1.0-plugins-base|good|bad|ugly` (the x264 encoder is in `ugly`), `gstreamer1.0-x`, `gstreamer1.0-pulseaudio`, `xdotool`, `wmctrl`, `xclip`; the pinned neko v3 release binary for the architecture, checked against its SHA-256, at `/opt/neko/bin/neko`; and neko's Xorg input driver from the same image, at `/usr/lib/xorg/modules/input/neko_drv.so` (built against the same xorg-server the snapshot installs).
Fortress moves into the same unit from `1000-playwright-fortress.sh`, which is deleted; the unit keeps `DWT_SKIP_BROWSER_UNIT`.
The image bakes nothing new, so the first boot of an existing workspace installs the stack through `env-converge` and the service answers `starting` with "Installing the screen" until the unit is satisfied.
The package `system/apps/screen` is a uv tool like every app, with `app.toml`:

```toml
name = "screen"
display_name = "Screen"
icon = "icon.svg"
critical = false
stop_when_no_windows = false
priority = "screen"
launcher_rank = 30
system = true

[default_shortcut]
launch = "open"
mode = "focus"

[[launch_paths]]
id = "open"
label = "Screen"
path = "/"

[[launch_paths]]
id = "browser"
label = "Browser"
path = "/launch/browser"
method = "POST"
params = [{name = "url", label = "Start page", required = false}]

[wiring]
programs = ["screen-xorg", "screen-session", "screen-pulse", "screen-neko", "screen-watch"]

[[message_handlers]]
type = "open:url"
path = "/api/open-url"
```

## 19. Contract and document changes

- `contracts.md`: section 2 (the built-in manifests table: `screen` in, `browser` out), section 4.2 and 8 (`FULLSCREEN`), section 10 (the fullscreen frame), section 11 (no new tokens), section 12 (`data-window-state="FULLSCREEN"`, `data-fullscreen-strip`).
- `concepts.md`: decision 17 superseded; the placement states list.
- `plan-desktop-interface.md` 9.4: the browser's lifetime paragraph replaced by the Screen's.
- `window-bound-resources.md` and `stop-when-no-windows.md`: a status line each noting the browser's supersession.
- `workspace_layout/README.md`, the `manage-desktop` skill, `system/apps/README.md`, `system/services/README.md`, `docs/system/workspace-internals.md`, `system/services/oom_priority/README.md` (the band table and "The Chromium exception" now under the Screen), `system/libs/bootstrap/README.md` (the unit), and the changelog entries of every package touched.

## 20. Testing

- `screen` package: the lease state machine (ported browser tests plus the human-takes-from-human and nobody-to-implicit-host cases), connect-info aggregation over fake brokers (both, one, none, slow), size quantization and the mode name, profile choice from aggregate state, the xorg.conf and neko.yaml renderers, the identity-to-session mapping, the CDP proxy (ported), the fleet routes (ported), the window-title poller over fake `wmctrl` output.
- Integration, inside the workspace image: the group starts on first demand, neko answers health, a session token is minted, a screenshot is taken, `screen run xeyes` appears in `windows`, a member's death stops the group and records the event, Quit stops everything, a second open restarts it.
  These need Xorg dummy and neko in the image, which the env.d unit installs; the CI snapshot producer runs it.
- Shell: the `FULLSCREEN` state in `desktop_document_test.py` and the frontend geometry tests through the shared vectors, the placement round trip, the title-bar and window-menu rows, the hover strip (`Window.test.ts`), the iframe attributes (`livePages.test.ts`), `workspace-layout place --state fullscreen` (`cli_test.py`).
- Share gateway: the rendered `tcp` proxy, the loopback `connect-info` route, the credential fetch and refresh over a fake connector.
- Manifests: `system/test_app_manifests.py` for the table and the band.
- Acceptance, through the real desktop on local Docker (Playwright with the Chrome channel): open the Screen, see a frame, take control, type into a terminal on the Screen and see the text, open the browser launch path with a URL and see the tab, close the window and see the stack still running, quit and see it gone.
- Manual release checks: the lab harness's click-to-photon on the staging tier through the desktop and through a share; immersive mode on the three desktop platforms (the integration spec, section 8).

## 21. Implementation order

Each step leaves the tree green on the branch; nothing merges until the last.

1. The package skeleton, the env.d unit, the four programs, the watcher, start on demand, `GET /api/state`, the viewer showing a frame through the service's proxy over loopback (`connect-info` empty, the server's own candidate used as given).
2. Sessions and identity, the lease and its projection, the strip, agent verbs and the `screen` command.
3. Sizing, profiles, visibility, audio.
4. `connect-info` through the desktop (needs the mngr branch's published ports and routes), candidate rewriting, direct-first and relay.
5. The share gateway's part and the relay `tcp` proxy (needs the connector's `media_port`).
6. `FULLSCREEN` in the shell, immersive mode through the embed contract, pointer lock plumbing.
7. Clipboard, files, mobile.
8. The browser's migration and the Fortress unit's move; the deletions; skills and documents.
9. Tests that need the whole, the acceptance test, the document changes of section 19.

## 22. Out of scope and follow-ups

- A damage gate for neko's encoder (pause while frames are identical), worth upstreaming; the 5 fps `idle` profile stands in.
- HiDPI: a 2x mode for a 2x viewer, past the 1080p cap.
- A view-only toggle per visitor.
- Profiling neko's gVisor floor (`server.pprof`).
- Self-hosted coturn (the TURN spec, section 9), if relay cost or abuse warrants it.
- Renaming the browser's profile and manifest paths under `screen`.
- An occlusion signal from the shell, so a Screen fully covered by another window stops streaming.

### Implementation notes

What the implementers found and verified where the tree departs from the text above; the text stands as the design, and these are the facts on the branch.

- The launch path of the Screen's own page is `screen`, not `open`: the manifest schema reserves `open` for the launch path the shell synthesizes, so the default shortcut is `{launch = "screen", mode = "focus"}` (section 18).
- The supervisord group is `screen-stack`, not `screen`: a program outside a group is its own group of that name, and the service program is `screen` (section 9.1).
- The service's own neko event session is a login with the admin password over the WebSocket, not the API token: the API-token session cannot open a WebSocket (section 6.2).
- neko sends `clipboard/updated` to the host session alone and with text only, so the viewer reads text and HTML back through the REST clipboard route (`GET /api/room/clipboard`) and sets them through its POST, since `clipboard/set` drops the HTML (section 13).
- File drop lands through the service, not neko: neko's `upload/drop` receives the files, but its drag emulation never maps its window on Xorg dummy with XFCE and the route answers 500, so the viewer posts dropped files to the service's `POST /api/drop`, which writes them into the XDG desktop folder (`XDG_DESKTOP_DIR` from `~/.config/user-dirs.dirs`, written by `xdg-user-dirs-update` when the session starts, else `~/Desktop`) and opens that folder in a Thunar window on the Screen, since the desktop's icons are off (section 4.3); they land in the folder rather than at the pointer.
- A tap on a phone reaches the Screen as a touch through neko's xf86 input driver, which the stack runs (the module from the pinned neko image, the socket under the runtime dir, the stack waiting for it before neko starts); a long press stays a right click (section 14).
- The pointer-lock control is hidden: neko v3 has no relative-motion event, only absolute `control/move`, so the plumbing waits behind `IS_RELATIVE_MOTION_SUPPORTED` (section 12).
- The share gateway's loopback route is `GET /connect-info` on its loopback port, as section 15 says, and the desktop's `media-endpoints` spells the protocol upper-case where the connector spells it lower-case; the service lowercases (section 5.2).
- neko stops the audio pipeline on its own while every session has audio disabled (measured: PulseAudio and the encoder at 0% with a muted viewer), so the null-device fallback of section 8.3 is not needed.
- The bands as prescribed shed Chromium's renderers before the programs on the Screen: `AGENT_SUBPROCESS` (900) sits below the shared-browser band (910 to 1000), so under pressure the order is Chromium's renderers, then programs on the Screen, then the stack (section 9.4).
- `GET /json/version` and `/json/list` are served by the CDP proxy on `127.0.0.1:8083`, not by the service on 8090 (section 10.1).
- The acceptance test of section 20 runs on a developer's machine only (`system/apps/screen/test_screen_acceptance.py`, which needs the mngr monorepo for the Imbue Studio app); CI runs the in-image integration suite.
- The screencast's rate is 5 fps (section 5.4) because each frame is a full JPEG of the display and the workspace is CPU-bound, and the fallback is for seeing the Screen, not for motion; the strip's "Details" toggle names the path in use from the peer's statistics.
- Relay servers get an eight-second bound in `connect-info`, candidates the two seconds of section 5.2: the desktop's first `ice-servers` answer mints a credential through the connector and Cloudflare, which took longer than two seconds and left the first relay attempt without servers.
- The browser opened from the Screen itself is the fleet's (section 16): the service renders XFCE's `helpers.rc` and a `WebBrowser` helper, and `mimeapps.list` with an application entry for `http(s)` links, into the home at each stack start, all naming `screen-browser-open` (`screen browser open [URL]`, the launch path), which the env.d unit installs on PATH and as the `x-www-browser` alternative.
- Chromium starts maximized (`--start-maximized` in `chrome_args`) rather than at a fixed size (section 16.1): the Screen is sized to the controller's window (section 7.2), often smaller than Chromium's default or the bounds its profile remembers, and xfwm4 never shrinks a window larger than the display, so a maximized window is what fills the Screen and follows its later sizes. The window manager still places it, and the user can restore it.

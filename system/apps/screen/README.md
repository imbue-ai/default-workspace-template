# screen

The Screen: the workspace's one streamed Linux desktop, shown as one window of the
desktop interface and driven by the human or by an agent, one at a time
(`docs/system/specs/screen/spec.md`). An X server with the dummy driver on display
`:99`, XFCE, PulseAudio with a null sink, and [neko](https://github.com/m1k1o/neko) v3
streaming it over WebRTC make up the stack; this package is the service that owns
them, the viewer page, the control lease, and the `screen` command agents drive it with.

It runs as the `screen` supervisord program (`system/supervisord.conf.d/screen.conf`)
from its own uv tool environment (`system/scripts/build_workspace.sh`), serving on
`http://127.0.0.1:8090`, the app's registered URL. The four programs of the stack form
the `screen-stack` supervisord group (`screen-xorg`, `screen-pulse`, `screen-session`,
`screen-neko`, one drop-in each, all `autostart=false`); the service starts them on
demand and the `screen-watch` event listener ends them together.

## The service

Identity comes from `X-Imbue-Identity` (a header-less request came over loopback and is the
owner). A visitor, an identity whose `owner` is false, may use only the viewer's routes (the
page and its assets, `/health`, `/api/schema`, `/api/state`, `/api/start`, `/api/connect-info`,
`/api/ws`, `/api/session`, `/api/control/take` and `/return`, `/api/input-seen`, `/api/screen-size`,
`/api/clipboard/image`, `/api/drop` and `/browsers/<name>/pending-url/cancel` with a visitor's
session id, and `/neko/`), where the lease arbitrates: a paste and a drop need the session in
control, a cancel the session in control or the owner. Naming the owner's session on those (its
id is in the state document while the owner controls) answers a visitor 403, and so does every
other route (`/api/run`, the agent verbs, the screenshot, the window tools, a session-less
`/api/screen-size`, `/launch/browser`, `/api/open-url` and the rest of the `/browsers` API).

- `GET /`: the viewer page (the frontend's build, `src/screen/static/index.html`; a
  placeholder until it is built), with its bundle under `/assets/`. A page load starts
  the stack when it is not running.
- `GET /api/state`: `{"lifecycle", "install_reason", "stop_reason", "title", "video_id",
  "control", "screen_size", "chromium"}`. `lifecycle` is `installing` while the env.d
  unit has not installed the stack, `starting` while the group comes up, `running`,
  or `stopped` with the `stop_reason`. `title` follows the frontmost X window ("Browser"
  for a Chromium window, the window's title otherwise, "Screen" for an empty desktop);
  `video_id` is the profile every viewer selects (below); `chromium` is the browser's
  `{"state", "name", "pending_url"}` (see "The browser").
- `GET /api/ws`: the service's own events to viewers: `state` (the document above, on
  connect and on every change), `profile` when the chosen video id changes, `stopped`
  with a reason, `handoff_request` when an agent hands the Screen to the human.
- `POST /api/screen-size`, body `{"width", "height", "session_id"?}`: the controller's
  stage size in CSS pixels. The service rounds each dimension down to a multiple of 16
  (a dimension at or past the cap is the cap itself, since 1080 is not one), clamps to
  640x480 through 1920x1080, and when that differs from the current mode adds the RANDR
  mode `<w>x<h>_60` with `xrandr --newmode <cvt modeline> --addmode <output>` unless
  the output already advertises it, then has neko select it (`POST /api/room/screen`),
  which restarts the capture and broadcasts `screen/updated`. A report whose
  `session_id` is not the controlling human's answers `{"applied": false, "reason":
  "not the controller"}`; a report with no session is the `screen size` command's and
  is applied. Added modes are recorded in `data/.state/screen/modes.json` for the
  stack's life and forgotten when it ends.
- `POST /api/input-seen`, body `{"session_id"}`: the viewer's input relay reporting
  activity (throttled to one per two seconds client-side), the profile chooser's clock.
- `POST /api/clipboard/image`, multipart `session_id` and `image` (an `image/...` part; the whole
  body is capped at 10 MiB, refused 413 as it is read, chunked or not): the controller's pasted
  image, placed on the X clipboard of `:99` with `xclip` (local to remote, spec section 13). A
  session that does not control the Screen is refused.
- `POST /api/drop`, multipart `session_id` and `files` (the whole body is capped at 64 MiB, refused
  413 as it is read, chunked or not): the
  controller's dropped files, written into the Screen's desktop folder (below) under their own
  names (a clash takes `-1`, `-2`, ...; a name with a path separator or a leading dot is refused);
  answers `{"directory", "saved"}`. A session that does not control the Screen is refused.
- `POST /api/start`: start the stack (the stopped overlay's Start button).
- `POST /api/session`: a neko session for the requester's identity (`X-Imbue-Identity`;
  a header-less request is the owner): the service logs the human in to neko with the
  user password and a display name from the identity and answers
  `{"session_id", "token", "name", "is_owner"}`; the viewer uses the token under `/neko/`.
- `POST /api/control/take` and `POST /api/control/return`, body `{"session_id"}`: the
  human verbs for that session.
- `POST /api/control/acquire` (body `{"reclaim"}`), `/release`, `/handoff` (body
  `{"reason"}`): the agent verbs, with `X-Mngr-Agent-Id` and `X-Mngr-Agent-Name` as the
  fleet API has them.
- `GET /api/screenshot?quality=N&format=png|jpeg`: the display, from neko's admin
  screenshot route (PNG by default).
- `GET /api/windows` and `POST /api/windows/<id>/activate`: the X window list and
  raise, through `wmctrl`.
- `POST /api/run`, body `{"command": [...]}`: start a command on the Screen with
  `DISPLAY=:99`, detached, in the `AGENT_SUBPROCESS` band; answers `{"pid"}`.
- `GET /api/connect-info`: `{"ice_servers", "candidates", "expires_at"}`, where the Screen's
  media port is reachable from this client and the relay servers to use (see
  "Connectivity"); `?relay=1` is the only time ICE servers are asked for.
- `POST /launch/browser`, body `{"url"?}`: the `browser` launch path; it starts the stack
  when it is down, starts Chromium when it is not running, opens `url` as a new tab in
  front when given, raises Chromium's window, and answers `{"path": "/"}`, the Screen's
  one page. A refusal (a `url` that is not an absolute http(s) URL, Chromium not
  installed) carries its reason under `detail`.
- `POST /api/open-url`, body `{"client_id", "url"}`: the `open:url` message handler. A
  link to a local address (`localhost`, `127.0.0.1`, `[::1]`, `*.localhost`) clicked in
  the workspace opens as a tab in Chromium, raised, and the Screen's page is shown to the
  client that clicked (the shell's `show` op); while an agent drives the Screen the page
  is held instead (below).
- `/browsers`, `/browsers/<name>/...`: the migrated fleet API the `agentic-browser-fleet`
  command drives (see "The browser").
- `/neko/...`: the reverse proxy to neko's HTTP and WebSocket on `127.0.0.1:8091`, with
  a rate limit on `/neko/api/login`.
- `GET /_static/app_contract.js` and `/_static/context_menu.js`: the shell's modules,
  served from this origin as every app serves them.
- `GET /health`: `{"status", "lifecycle"}`.
- `GET /api/schema`: the OpenAPI document of this API, built from the routes (the manifest's
  `api_schema_path`; `system/libs/app_schema`), for an agent that reaches the Screen from inside
  the workspace or over a share.

## The browser

The browser is a launch path of the Screen, not an app (spec section 16): one Fortress
Chromium with one profile, started by the `browser` launch path, `screen run`, or an
agent's `agentic-browser-fleet new`, its windows ordinary X windows the window manager
places (the user opens as many as they like; nothing pins or closes them; Chromium starts
maximized, so its window fills whatever size the controller's window gave the Screen and
follows the Screen's later sizes), and never
stopped when its windows close (the user quits it from inside, `POST
/browsers/<name>/stop` ends it, or Quit ends the stack). The fleet keeps the browser
app's shape: browsers by name (`browser-1`, minted as the first free `browser-<N>`), a
cap of one launched browser, the profile under `$MNGR_HOST_DIR/browser-profiles/`
(`browser-use-user-data-dir-<name>`, the prefix an upgraded workspace's logins are found
under), and the tab manifest at `data/.apps/browser/instances.json`, checkpointed every
ten seconds while the browser runs, on every stop, start and close, and before the
service stops the stack (`ScreenStack.on_stopping`, while Chromium can still be asked).
When the stack ends (Quit, memory pressure, a member dying) the X server takes Chromium
with it and the browser is `stopped` for that stack life; the next stack start relaunches
it on its saved tabs unless the user had stopped it on purpose (the manifest's `stopped`
flag), and a workspace whose manifest is lost registers the profiles on disk as stopped
browsers rather than launching a Chromium nobody asked for. Chromium's processes are
remapped into the shared-browser band by the band sweep (below).

Opening a browser from the Screen itself lands in the same Chromium. XFCE's "Web Browser"
launcher and `exo-open --launch WebBrowser` resolve through `~/.config/xfce4/helpers.rc` to a
helper, and GTK programs resolve an `http(s)` link through `~/.config/mimeapps.list` to an
application entry; the service renders both, with the helper and application `.desktop` files
under `~/.local/share/`, at each stack start (`xfce_helpers.py`, merging with what the files
already hold). They name `screen-browser-open`, the Screen package's one-line script
(`scripts/screen-browser-open`, `exec screen browser open "$@"`) that the env.d unit installs
at `/usr/local/bin/screen-browser-open` and registers as the `x-www-browser` alternative, so
`sensible-browser` lands there too. `screen browser open [URL]` posts to `/launch/browser`.

The fleet API (`/browsers`): `GET /browsers` lists the browser with its lifecycle
(`init`, `running`, `crashed`, `stopped`), the Screen's lease as the fleet CLI reads it
(`controller` is `human`, `agent` or `nobody`), and its tabs; `POST /browsers` (`name`
and `url` optional) answers the one browser like the launch path does, or creates a
named one (400 for a bad name or URL, 409 for a duplicate or a full fleet, 503 while
Chromium is installing); `DELETE /browsers/<name>` ends it and deletes its profile;
`POST .../stop` and `.../start` end and relaunch Chromium with the tabs kept;
`POST .../close-tab` closes the tab in front (the last tab is replaced with a blank one
so the window stays); `POST .../pending-url/cancel` drops a held page (`session_id` in the
body or the query: the session in control may, and so may the owner, with or without a
session, since agents and the shell call it over loopback with none); `GET .../attach`
issues an agent its attach URL; `POST .../acquire`, `.../release` and `.../handoff` are
the Screen's lease verbs under the fleet's paths, so the fleet CLI's exit codes hold.

Agents drive Chromium through the CDP proxy on `127.0.0.1:8083` (its own port: the
service's is published to the desktop and an unauthenticated CDP endpoint must not travel
with it), at `ws://127.0.0.1:8083/<name>/<token>` with the token `attach` minted for one
agent. Every frame is gated on the Screen's lease: the token's agent may drive while it
holds the lease (a frame touches the lease's idle clock and the profile's input clock),
a free Screen is taken on the first frame, and a human holding control refuses the frame
with a CDP error that names `agentic-browser-fleet ls`, never closing the socket (a
dropped socket poisons a playwright-cli session for good). `Browser.close`, the incognito
context verbs, `Emulation.setDeviceMetricsOverride` and the download redirects are
refused outright; closing the last page is refused; `Browser.setWindowBounds` is allowed,
since the window manager owns the windows now. Discovery (`/json/version`, `/json/list`,
with and without a trailing slash) is served with the proxy's URLs substituted and
`Connection: close`. After a forwarded command the view follows the tab the agent
addressed.

The held-URL rule: a page the human asked to open (`open:url`, or the launch path's
`url`) while an agent drives the Screen is held rather than opened over the agent's
work; the viewer's agent overlay names it with "Take control and open it" and "Cancel",
and it also opens once the agent releases or hands off, or when Chromium finishes
launching. Agents queued for the Screen are woken through the chat messenger as the
browser fleet's were.

## Bands

Programs started from the Screen itself (the XFCE menu, a terminal on it) inherit the
session's band, the stack's own, so the service runs a raise-only sweep every ten
seconds (`band_sweep.py`, spec section 9.4): every descendant of `screen-session` whose
executable is not one of the session's own (`startxfce4`, `xfce4-session`, `xfwm4`,
`xfce4-panel`, `xfdesktop`, `xfsettingsd`, `xfconfd`, the D-Bus daemon) is raised to
`AGENT_SUBPROCESS` (900), and Chromium's processes, under the session or under the
Chromium the fleet launched, are remapped into the shared-browser band (910 to 1000,
renderers at the ceiling) as the browser app's `oom_retag` did; a launch or a new tab
kicks a short burst of sweeps, since Chromium's processes self-write their values over
the seconds after. The sweep only ever raises, so repeating it writes nothing more.

## Profiles, visibility and audio

Three H.264 pipelines in `neko.yaml` (spec section 8.1): `idle` 5 fps 800 kbps
ultrafast one thread, `watch` 20 fps 2500 kbps veryfast four threads, `control` 30 fps
4000 kbps veryfast four threads, all constrained baseline with `tune=zerolatency`, a
one-second keyframe interval and `show_pointer` off. The service chooses one for
everyone from the lease and the input clock: `control` while a human holds control and
input arrived in the last ten seconds, `idle` while nothing has arrived for a minute
and no agent holds the lease, `watch` otherwise (a stack start counts as input, so a
fresh Screen streams at `watch` for its first minute). Input arrives through
`POST /api/input-seen` from the controller's viewer and through every agent command.
The choice is re-evaluated every second, on every input and on every lease change,
announced as `profile` on `/api/ws`, and every viewer selects it with `signal/video`,
so neko runs one encoder.

A viewer is visible when its document is not hidden, the shell said `shown` (or never
said `hidden`), and its stage intersects the viewport; it re-evaluates on
`visibilitychange`, the shell's messages, intersection changes, and every 1.5 seconds.
Hidden for five seconds it sends `signal/close` (the WebSocket, the session and the
lease stay); visible again it sends `signal/request`. Neko stops the video encoder when
the last session has closed its media. Input is sent only while this viewer is the
controller and visible; a controller that goes hidden keeps the lease.

Every session requests video with `audio.disabled: true`; the strip's mute control
sends `signal/audio` with `disabled: false` for this session alone and unmutes the
video element. Measured in a local Docker workspace (neko v3.2.0): with one viewer
streaming and audio disabled, `pulseaudio` sits at 0% CPU and neko at the video
encoder's cost alone (3.6% at `idle`); enabling audio from the strip puts `pulseaudio`
at 1% and neko at 7%, and closing the viewer takes both back to 0%. So neko does stop
the audio pipeline while every session has audio disabled, and the spec's fallback (a
null device until a session enables audio) is not needed. The capture device is the
null sink's monitor in any case.

## Clipboard, files and the phone

`xclipboard.py` (the browser app's bridge, carried over) owns the X clipboard: `xclip`
sets and reads the CLIPBOARD selection and an XFixes watcher on `:99` notices every
ownership change while the stack runs. neko sends `clipboard/updated` to the host
session alone and with text only, so the directions are:

- Remote to local: on `clipboard/updated` the controller's viewer reads text and HTML
  from neko's `GET /api/room/clipboard` (host only) and writes them with the Clipboard
  API; when the watcher sees an image target the service broadcasts `clipboard`
  (`{"kind": "image"}`) on `/api/ws` and the controller's viewer fetches
  `GET /neko/api/room/clipboard/image.png` and writes it. A write that fails for want of
  focus runs on the next focus.
- Local to remote: on focus and on paste, while this viewer holds control, the viewer
  reads the local clipboard; text and HTML go to neko's `POST /api/room/clipboard` (its
  `clipboard/set` message drops the HTML), an image to `POST /api/clipboard/image` as this
  session, which the service accepts from the session in control alone (a visitor in control
  included). A paste chord (Ctrl+V, Cmd+V) is
  left to the browser so its `paste` event carries the data; once the remote clipboard
  holds it the viewer taps V with Ctrl held on the remote, and a paste that did not cross
  says so in the strip. What was just received is never echoed back, by content hash.
- `keyboard/map` goes once per neko connection and again when this session takes
  control (only the host may set it), with the layout `navigator.language` suggests,
  `us` by default.

A file dropped on the viewer by the controller goes to the service's `POST /api/drop`, which
writes it into the Screen's desktop folder: the XDG desktop directory `xfdesktop` shows as icons
(`XDG_DESKTOP_DIR` in `~/.config/user-dirs.dirs`, the file `xdg-user-dirs-update` writes when
the session starts, else `~/Desktop`; the service and the session share one home, so
`desktop_files.py` resolves it the same way), created when missing. The strip then says where
the files landed. neko's own `upload/drop` route is not used: verified in a local Docker
workspace (neko v3.2.0), the files reach neko but its drag emulation (a GTK popup it drags
from the top-left corner with its own pointer moves) never maps its window on this stack and
the route answers 500 after neko's one-second wait, so a dropped file lands as a desktop icon
rather than at the pointer.

Under the phone layout (a phone-sized viewport with a finger as the primary pointer, the shared
`PHONE_MEDIA_QUERY` and `TOUCH_MEDIA_QUERY` both; the shell gives pages no phone signal, and a
desktop window that is merely small is not a phone: it reports its size and the Screen resizes to
fit it) the Screen keeps its size and the viewer offers: a one-finger drag is a touch held and
moved on the Screen, so a page scrolls under the finger as on any phone (once the video is
pinch-zoomed the drag pans the view instead; a pinch zooms the drawn video, the viewer's own
transform, never a resize), a tap is a left click and a long press a right click at the mapped
point, a two-finger drag scrolls in wheel steps, a bar offers Ctrl, Alt and Shift (each latching
for the next key), Esc, Tab, Scroll Up and Scroll Down (three wheel steps each, for programs that
ignore touch), and a Keyboard button focuses a hidden input whose `input` and `keydown` events
become neko key events. Taps and drags reach the Screen as touches: neko's xf86 input driver
(`neko_drv.so`, the `screen_touchscreen` device `xorg.conf` declares, its socket
`/tmp/screen-runtime/xf86-input-neko.sock`) is a direct-touch device whose pointer
emulation also moves the core pointer, and the viewer sends `control/touchbegin`,
`control/touchupdate` and `control/touchend` at the mapped points with the finger's pressure
when neko's `system/init` says `touch_events` (without the driver a drag pans the view as a tap
is a click); a long press stays a right click (X touch has no second button). The stack waits
for the driver's socket before starting neko, which dials it once and exits without it.

## The pointer

No cursor is in the video (`show_pointer` is off in every pipeline). neko v3.2.0 sends the X cursor
over the WebRTC data channel it opens on each peer, not over the WebSocket: on the channel's open and
on every cursor change, an `OP_CURSOR_IMAGE` (0x02) message with the width, height, hotspot and PNG
(`server/internal/webrtc/payload/send.go`, `server/internal/webrtc/manager.go`), and to peers that are
not the host an `OP_CURSOR_POSITION` (0x01) with the pointer's x and y. The viewer reads the channel
(`frontend/src/remote_cursor.ts`) and, while this session controls (or may take control, with nobody
holding it), gives the stage's CSS `cursor` the image with its hotspot as a data URI, shrunk with the
drawn video when the stage shows the desktop smaller than 1:1 and capped at 128 px a side (past which
Chrome and Firefox ignore a cursor image), with the browser's own pointer as the fallback when the image
cannot be drawn. A viewer that is watching keeps the default pointer; the position and neko's inactive
cursors (`session/cursors`) are not drawn. No picture has a pointer painted into it: neko's global
`capture.video.show_pointer` (on by default, and the flag the JPEG screencast pipeline takes, where the
per-pipeline flag covers the WebRTC pipelines alone) is set off in `neko.yaml` too, so in the screencast
the browser's own pointer is the one pointer.

The viewer carries the pointer-lock plumbing of spec section 12 (`frontend/src/pointer_lock.ts`:
a strip control that locks the pointer on the stage, the relative motion it yields while locked,
Escape releasing it in the browser itself), but the control is hidden: neko v3 has no
relative-motion event. Its `control/move` carries an absolute `{x, y}` only
(`server/pkg/types/message/messages.go` defines no delta message for pointer motion and
`server/internal/websocket/handler/control.go` no handler for one; the only deltas are the
scroll's), and integrating deltas into absolute moves breaks as soon as a relative-motion program
warps the pointer itself, which is what such programs do. `IS_RELATIVE_MOTION_SUPPORTED` turns
the control on once neko gains such an event.

## Connectivity

The stream is ICE over neko's UDP and TCP muxes on port 59000 inside the container (spec
section 5). Every create template publishes that port (`publish__extend = ["59000/udp",
"59000/tcp"]`; the `modal` template publishes only `59000/tcp`, since Modal tunnels carry TCP
alone), so the host reaches it at a host port the provider picked; a shared
workspace's visitors reach it through the relay's `screen-media` tcp proxy or through TURN.
Neko itself is told nothing about any of this: its candidates carry the container's own
address, and the viewer rewrites them.

`GET /api/connect-info` asks two brokers in parallel, each bounded (two seconds for candidates,
eight for relay servers: they are asked for only once the direct path has failed, and the
desktop's first answer mints a credential through the connector and Cloudflare, which has taken
longer than two seconds), and answers the union:

- The desktop, through the latchkey gateway's `minds-api-proxy`
  (`$LATCHKEY_GATEWAY/minds-api-proxy/api/v1/agents/$MNGR_AGENT_ID/media-endpoints`, and
  `.../ice-servers` only with `?relay=1`), with the gateway password and permissions
  override mngr injects into the service's environment. Without a gateway or an agent id
  in the environment the desktop is not asked.
- The share gateway's loopback `GET http://127.0.0.1:8791/connect-info`
  (`system/services/share_gateway/README.md`), which answers only while the workspace is
  shared: the box's own published endpoints, one tcp candidate per relay at the share's
  media port, and the relay credential the gateway holds.

Candidates are filtered to guest port 59000, de-duplicated, and spelled with a lower-case
`protocol` (`udp` or `tcp`; the desktop spells it upper-case, the connector lower-case, and
the service compares case-insensitively). `ice_servers` is the brokers' union, passed to the
viewer unchanged, and `expires_at` the earliest expiry. A broker that is slow, unreachable
or refusing is simply absent; with both absent the answer is empty lists, which the viewer
reads as "use the server's own candidate". The answer is never written to disk and the
route forbids caching: a relay credential is a bearer token.

The viewer (`frontend/src/webrtc.ts`, `candidates.ts`, `ice_policy.ts`) fetches
`connect-info` before signaling, builds its peer connection with the ICE servers and
`iceTransportPolicy: "all"`, and for every server candidate neko sends emits one copy per
`candidates` entry of the same protocol with the address and port replaced and a distinct
foundation, dropping the original (unless `candidates` is empty). A connection that is not
`connected` within four seconds, or fails later, fetches `connect-info?relay=1`, hands the
servers to the peer and asks neko for an ICE restart (`signal/restart`); the viewer then
remembers `screen:needs-relay` in `localStorage` and fetches with `relay=1` first on the
next load. When the relay attempt fails the same way, the viewer shows neko's JPEG
screencast (`capture.screencast`, 5 fps at quality 60, polled at
`/neko/api/room/screen/cast.jpg`) with input still over the WebSocket, and the strip's row
carries a loud alert ("Low-quality picture: no direct or relay path connected. Retrying.")
with a "Retry now" button. From the screencast the viewer tries WebRTC again on its own,
15 s after the fall, then 30 s, then every 60 s, each time with fresh `connect-info?relay=1`
(the usual cause is a relay credential that was not ready in time); the screencast keeps
showing the Screen until a retry's peer connects. The strip's "Details" toggle opens a
panel over the stage naming the path in use (direct or through a relay, udp or tcp, the
remote address, from the peer's `getStats()`), the endpoints `connect-info` offered, the
relay server count, the Screen's size and profile, and the picture's size, rate, bitrate and
round trip; the viewer also logs one "media path" line to the console per connection.

## The lease

`control.py` holds the lease (spec section 6): `controller` (`human`, `agent`,
`nobody`), the agent holding it, whether the human took control explicitly, a FIFO wait
queue and a resume queue for agents, a 60-second idle TTL on an agent's lease and a
12-second claim window for a granted one. Any human may take control, which preempts an
agent (queued to resume first) or another human; agents never preempt a human and queue;
`reclaim` is the one exception, for an agent the user told to continue. The lease is
projected onto neko: `locked_controls` on while an agent holds it, off otherwise, and
the human controller made host with `control/give`. The service's own admin WebSocket
session watches `control/host` and `session/*` so a human taking an uncontrolled Screen
is recorded. The lease is checkpointed to `data/.state/screen/control.json`.

## Files

`data/.state/screen/`: `xorg.conf`, `neko.yaml` (mode 600, this start's passwords and
API token), `pulse.pa`, `control.json`, `modes.json` (the RANDR modes added this stack
life), `events/screen/events.jsonl` (the `screen_stopped` events the watcher appends).

## The commands

`uv run --no-sync screen` is the agent-facing command (spec section 17): `run`,
`windows`, `activate`, `shot`, `size [WxH]` (reads the size, or sets it through
`POST /api/screen-size` as the controller's viewer would), `control
acquire|release|handoff`, `open`, `browser open [URL]` (the browser launch path). Its
exit codes are the fleet CLI's: 0 ok, 1 error, 2 preempted (a human has or took
control; end the turn), 3 busy, 4 timed out, 64 usage, 69 no service.

`uv run agentic-browser-fleet` (`fleet_cli.py`, the `agentic-browser-fleet` skill) owns
the browser: `ls [--include-tabs]`, `new [name] [--url]`, `close <name>`, `acquire <name>
[--reclaim]`, `release <name>` (alias `unlock`), `handoff <name> "<reason>"` (alias
`request-human`), against the fleet routes, with the same exit codes. `new` and `acquire`
open the Screen's window minimized through the shell's `open` op and print the
`playwright-cli attach --cdp=` line; `handoff` shows the window, since the human has to
act in it.

## Packaging

The env.d unit `system/scripts/env.d/2000-screen.sh` installs the stack's apt packages
from the pinned snapshot, the pinned neko binary at `/opt/neko/bin/neko` and neko's Xorg
input driver at `/usr/lib/xorg/modules/input/neko_drv.so` (both from the pinned neko base
image, whose driver is built against the same xorg-server the snapshot installs),
Fortress, which moved there from the browser's unit, and the `screen-browser-open`
command with its `x-www-browser` alternative. Until it is satisfied the service
answers `installing`.

The frontend (`frontend/`) is a member of the npm workspace at `system/package.json`,
built into `src/screen/static/` by `npm run build`; its tests run with `vitest run`.

```bash
# Backend, from the repo root
uv run screen-service

# Frontend
cd system/apps/screen/frontend && npm run dev

# Tests
cd system/apps/screen && uv run pytest
```

## Tests

The unit tests run anywhere. `test_screen_integration.py` runs the real stack (Xorg, PulseAudio,
XFCE, neko and Fortress as subprocesses under the service) and skips where the env.d unit has not
installed it; the CI job that runs the suites inside the workspace image fails on that skip, which
is how a missing install is noticed there. To run it by hand in a workspace, quit the Screen first
(the suite needs display `:99`) and run `uv run --no-sync pytest system/apps/screen/test_screen_integration.py`
from the repo root as root.

`test_screen_acceptance.py` is the acceptance test of spec section 20 through the real desktop:
Imbue Studio's Electron app creates a local Docker workspace from this tree (a plain clone of the
committed branch, so commit first), and Playwright drives it over CDP to open the Screen, see a
frame, take control, type into a terminal on the Screen, open the browser launch path with a URL,
close the window with the stack still running, and quit with everything gone. It needs the mngr
monorepo (synced, with Electron installed under `apps/minds`), Docker on this machine, a display
(the Electron window appears on it), a Chromium to show the viewer in (Electron exposes neither the
Screen's nested frame nor a new page to Playwright: the one `SCREEN_ACCEPTANCE_CHROMIUM` names, else
this venv's Playwright build, else the newest build under `~/.cache/ms-playwright`), and, while the
tree pins the private mngr repo (`python3 system/scripts/set_mngr_pin.py --check` says `internal`),
`MNGR_INTERNAL_GIT_TOKEN` in the environment (the read-only credential the image's build then needs
to fetch mngr; the mngr monorepo's `apps/minds/docs/dwt-mngr-pin.md` says where it comes from); it
skips at collection without `MNGR_WORKTREE`, so CI never runs it. Everything it makes is torn down at
the end (the workspace through `mngr destroy`, the app's processes, the throwaway minds root under
pytest's temporary directory), and its screenshots are left under that directory for a look:

```bash
MNGR_WORKTREE=/path/to/mngr uv run --no-sync pytest -q -p no:cacheprovider \
  system/apps/screen/test_screen_acceptance.py --basetemp=/var/tmp/screen-acceptance
```

A cold image build takes a quarter of an hour or more; with Docker's layer cache warm the whole run
is a few minutes of create plus a few of the journey.

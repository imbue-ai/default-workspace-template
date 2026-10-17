The Screen app (`docs/system/specs/screen/spec.md`, implementation steps 1 and 2): the workspace's one streamed Linux desktop as a window.

- The `screen-service` daemon on `127.0.0.1:8090` renders `xorg.conf`, `neko.yaml` (mode 600, with that start's passwords and API token) and the PulseAudio script under `data/.state/screen/`, starts the `screen-stack` group on demand (a page load, the Start button, the `browser` launch path, `screen run`, an agent's `acquire`) and answers `starting` until neko's health route does; `GET /api/state` carries the lifecycle (`installing` while env-converge has not installed the stack), the lease, the frontmost window's title and the display mode; `/api/ws` pushes `state`, `stopped` and `handoff_request`.

- Sessions and identity: `POST /api/session` logs the requester in to neko with the user password and a name from `X-Imbue-Identity` (the name, else the email's local part, else Owner or Guest); the viewer logs out on `pagehide` and the service deletes sessions disconnected for ten minutes.

- The control lease (`ScreenControl`), the browser fleet's state machine with `acquire`, `release`, `take_control`, `handoff` and `return_to_agents`, a FIFO wait queue and a resume queue, the 60-second idle TTL and the 12-second claim window, checkpointed to `control.json` and projected onto neko (`locked_controls` while an agent holds it, the human made host otherwise, a human taking an uncontrolled Screen learned from `control/host`). Human routes `POST /api/control/take` and `/return`; agent routes `/acquire`, `/release`, `/handoff` with `X-Mngr-Agent-Id`.

- `GET /api/screenshot` (PNG by default, from neko's admin screenshot), `GET /api/windows` and `POST /api/windows/<id>/activate` through `wmctrl`, `POST /api/run` launching a command with `DISPLAY=:99` in the `AGENT_SUBPROCESS` band, the `/neko/` reverse proxy (HTTP and WebSocket, with a rate limit on the login route), and `GET /api/connect-info` answering no endpoints yet.

- The `screen` command: `run`, `windows`, `activate`, `shot`, `size`, `control acquire|release|handoff`, `open`, with the fleet CLI's exit codes. The `screen-watch` event listener stops the rest of the group when a member exits, appends a `screen_stopped` event and tells the service.

- The viewer (`frontend/`, built into `src/screen/static/`): the video over neko's signaling with the server's candidate used as given, the strip (who has control, Take control, Give control back), the installing, starting and stopped overlays, the agent dim, mouse and keyboard input while this session controls, and the shell connection with `closeChord` sending Ctrl+W to the remote.

- Pointer lock (spec section 12): the viewer carries the plumbing (`frontend/src/pointer_lock.ts`: a strip control that locks the pointer on the stage, the relative motion it yields while locked, Escape releasing it in the browser itself), but the control is hidden, since neko v3 has no relative-motion event (`control/move` is absolute only); the README says so, and `IS_RELATIVE_MOTION_SUPPORTED` turns the control on once neko gains one.

Implementation step 3 (sizing, profiles, visibility, audio):

- `POST /api/screen-size` resizes the Screen from the controller's stage size: rounded down to 16 px (the cap itself at or past 1920 or 1080), clamped to 640x480 through 1920x1080, the RANDR mode `<w>x<h>_60` added with `xrandr --newmode`/`--addmode` from the package's own CVT modeline when the dummy output lacks it, then selected through neko. A non-controller's report is ignored; added modes are recorded in `data/.state/screen/modes.json` for the stack's life. `screen size WxH` sets it through the same route.

- The profile chooser: `control` while a human holds control and sent input in the last ten seconds, `idle` after a minute without input while no agent holds the lease, `watch` otherwise, fed by `POST /api/input-seen` from the controller's viewer and by every agent command; announced as `profile` on `/api/ws` and in `GET /api/state`, and selected by every viewer with `signal/video`.

- The viewer releases its media (`signal/close`) after five seconds hidden, judged from `document.hidden`, the shell's `shown`/`hidden` messages and an IntersectionObserver on a 1.5-second belt, and requests it again when visible; input goes only while visible and controlling. A phone never reports a size. A large mute control in the strip enables audio for this session alone (`signal/audio`); every session starts with audio disabled.

Implementation step 7 (clipboard, files, mobile):

- Clipboard both ways while this viewer controls: text and HTML through neko's clipboard route on `clipboard/updated` and on focus or paste; images remote-to-local through neko's `clipboard/image.png` when the service's X clipboard watcher (`xclipboard.py`, the browser app's bridge carried over) announces one on `/api/ws`, and local-to-remote through `POST /api/clipboard/image`, which places the image on `:99` with `xclip`. A paste chord sets the remote clipboard first and then taps V there. `keyboard/map` from `navigator.language`.

- Dropping files on the viewer uploads them through neko's `upload/drop` route at the pointer's Screen coordinates; neko receives them, but its own drag emulation does not complete on this stack yet (the strip reports the failed drop), which is a follow-up.

- Under the phone layout: pan and pinch-zoom of the drawn video, tap as click and long press as right click mapped into Screen coordinates, two-finger scroll, a modifier bar (Ctrl, Alt, Shift latching for the next key, Esc, Tab) and a Keyboard button raising the soft keyboard; the Screen is never resized from a phone.

Step 4, connectivity (spec section 5):

- `GET /api/connect-info` asks the desktop through the latchkey gateway's `minds-api-proxy` (`media-endpoints`, and `ice-servers` only with `?relay=1`) and the share gateway's loopback `GET /connect-info` in parallel with a two-second bound each, and answers the union: candidates filtered to guest port 59000, de-duplicated, protocol lower-case; `ice_servers` from every broker; the earliest `expires_at`. A slow, unreachable or refusing broker is absent; both absent is empty lists. The answer is held in memory only and the route forbids caching.

- The viewer rewrites every server candidate to the endpoints `connect-info` named (one copy per matching endpoint, address and port replaced, a distinct foundation, the original dropped unless no endpoint is known), builds its peer connection with the ICE servers and `iceTransportPolicy: "all"`, and escalates a connection that is not up within four seconds or fails later: `connect-info?relay=1`, the servers handed to the peer and neko asked for an ICE restart, with `screen:needs-relay` remembered in `localStorage` so the next load fetches with `relay=1` first. A relay attempt that fails the same way shows neko's JPEG screencast (10 fps, quality 60) with input still over the WebSocket, and the strip says so.

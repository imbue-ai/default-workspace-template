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

Step 8, the browser's migration (spec section 16): the browser app's history continues here.

- The browser is a launch path of the Screen: `POST /launch/browser` (body `url` optional) starts the stack when it is down, starts one Fortress Chromium when it is not running (or registers it to start with the stack), opens the start page as a new tab in front, raises Chromium's window and answers the Screen's one page. Chromium's windows are ordinary X windows the window manager places; nothing pins or closes them, and the browser is never stopped when its windows close. `GET /api/state` carries `chromium` (`state`, `name`, `pending_url`) and its `title` reads "Browser" for a Chromium window.

- The fleet API moved into the service under its old paths (`GET`/`POST /browsers`, `DELETE /browsers/<name>`, `.../stop`, `.../start`, `.../close-tab`, `.../attach`, `.../acquire`, `.../release`, `.../handoff`, `.../pending-url/cancel`), with the Screen's `ScreenControl` as the one lease behind the agent verbs; browsers keep their names (`browser-1`), the profile stays under `$MNGR_HOST_DIR/browser-profiles/` and the tab manifest at `data/.apps/browser/instances.json`, checkpointed every ten seconds, on every stop, start and close, and before the service stops the stack. A stack that ends takes Chromium with it; the next start relaunches the browser on its saved tabs unless the user stopped it on purpose, and a lost manifest brings the profiles on disk back stopped.

- The CDP proxy on `127.0.0.1:8083` is ported to synchronous websockets with its gate now over the Screen's lease: the token's agent drives while it holds the lease, a free Screen is taken on the first frame, a human in control refuses the frame without closing the socket. `Browser.setWindowBounds` leaves the block list; `Emulation.setDeviceMetricsOverride`, `Browser.close`, the incognito verbs and the download redirects stay blocked, and so does closing the last page.

- `POST /api/open-url` (the `open:url` handler) opens a local link as a tab in Chromium, raised, and shows the Screen's page to the client that clicked; while an agent drives, the page is held and the viewer's agent overlay names it with "Take control and open it" and "Cancel", opening it when the agent lets go.

- `agentic-browser-fleet` is a console script of this package now (`fleet_cli.py`), with its verbs and exit codes unchanged, opening the Screen's window minimized on `new` and `acquire` and shown on `handoff`; `ls` says "human (driving the Screen)" for an unpinned human in control.

- The service runs a raise-only band sweep every ten seconds (`band_sweep.py`, spec section 9.4): programs started from the Screen are raised to `AGENT_SUBPROCESS`, Chromium's processes are remapped into the shared-browser band as the browser app's `oom_retag` did, with a burst after each launch or new tab.

- The `open_popups_as_tabs` extension, `chrome_args`, `chrome_launcher` (Fortress's launcher waits up to 45 seconds for the debug port), the CDP client (synchronous) and the manifest and name modules moved here with their tests; the viewer-sizing, capture-backend and videopipe tests of the browser app are gone with the code they covered.

Step 9, the tests that need the whole (spec section 20):

- `test_screen_integration.py` sets its own timeout (the root config's ten seconds would end a stack start), marks the Fortress case `browser`, wires the browser fleet before the stack as the service does, and gains the Quit case: the service's shutdown ends every member and frees the display, and a fresh service's first page load starts a new stack.

- `test_screen_acceptance.py`, marked `acceptance`: Imbue Studio's Electron app creates a local Docker workspace from this tree and Playwright drives the whole journey of the spec's acceptance row (a frame, control, a terminal typed into, the browser launch path with a URL, close with the stack running, Quit with everything gone). It runs on a developer's machine with `MNGR_WORKTREE` naming the mngr checkout and skips at collection without it; the README has the recipe.

- The acceptance run passes end to end (2026-10-17, a local Docker workspace built from this branch, about seven minutes with the image's layers cached): the Electron app's create form to a booted workspace, the Screen opened from the launcher, a frame with a desktop's spread of grey levels, control taken by a click, `xfce4-terminal` typed into through the viewer (the typed command created its marker file), `workspace-layout open https://example.com` putting Chromium with "Example Domain" on the Screen with the title "Browser" and the tab listed by the fleet once env-converge had installed Fortress, the window closed with the stack still running, and Quit from the window menu ending the service and every member with the display gone. Electron exposes neither the Screen's nested frame nor a new page to Playwright, so the run shows the viewer in a Chromium of the operator's machine (`SCREEN_ACCEPTANCE_CHROMIUM`, else this venv's Playwright build, else the newest build under `~/.cache/ms-playwright`) at the same address with the desktop session's cookies; every mngr the app spawns reads the clone's settings, opted into pytest, through `MNGR_PROJECT_CONFIG_DIR` with the Docker provider enabled, and `MNGR_INTERNAL_GIT_TOKEN` is required only while the tree pins the private mngr repo.


The owner gate:

- A workspace visitor (an `X-Imbue-Identity` whose `owner` is false, admitted to the Screen's origin by a share) is refused with 403 on every route that acts on the Screen outside the lease: `POST /api/run`, the agent verbs `/api/control/acquire`, `/release` and `/handoff`, `GET /api/screenshot`, the window routes, `POST /api/clipboard/image`, a session-less `POST /api/screen-size`, `POST /launch/browser`, `POST /api/open-url` and the whole `/browsers` fleet API. The viewer's routes (the page, state, start, connect-info, the events socket, sessions, the human verbs, input-seen, a session's own size report and the neko proxy) still answer a visitor, who gets control through the lease as before, but only for a visitor's session: the human verbs, input-seen and a size report that name the owner's session (its id is in the state document while the owner controls) answer a visitor 403; a header-less request still reads as the owner, so agents and the shell over loopback are unaffected.

- The viewer offers the held page's Cancel (an owner-only verb) to the owner's session alone; a paste that the Screen refused, such as a visitor's image, says so in the strip instead of failing quietly.

- Programs started on the Screen (`screen run` commands, Chromium) no longer inherit the service's `LATCHKEY_GATEWAY_PASSWORD` and `LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE`; nothing launched there needs them, and an agent that does holds them in its own shell.

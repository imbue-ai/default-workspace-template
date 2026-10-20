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

- `test_screen_integration.py` no longer depends on the order its tests run in: every test takes a `running` fixture that starts the stack over the module's service life on demand (and builds a fresh life after a test quit the service), so each passes alone in its own CI sandbox.

File drop lands on the desktop:

- Dropping files on the viewer now posts them to the service's own `POST /api/drop` (multipart `session_id` and `files`, the controlling session alone, a visitor in control included) instead of neko's `upload/drop`, whose drag emulation never completes on this stack. The service writes them into the Screen's desktop folder, the XDG desktop directory `xfdesktop` shows as icons (`XDG_DESKTOP_DIR` from `~/.config/user-dirs.dirs`, else `~/Desktop`), creating it when missing, keeping each file's name, suffixing a clash (`report-1.pdf`), refusing names with path separators or a leading dot, and answering 413 past 64 MiB for one drop. The strip says where the files landed (`Saved to /home/user/Desktop: report.pdf`).

- `test_screen_integration.py` drops a file through the route in the workspace image and finds it in the desktop folder.

Lease arbitration for the paste and the held page's Cancel:

- `POST /api/clipboard/image` takes multipart `session_id` and `image` and the service accepts it from the session in control alone, so a visitor in control pastes images again; `POST /browsers/<name>/pending-url/cancel` takes `session_id` in its body or query and answers the session in control or the owner (a header-less caller over loopback, an agent or the shell, still works with no session). Both left the owner gate; a visitor's session that does not control gets the lease refusal (403, `only the session in control of the Screen may do this`), and naming the owner's session the owner-gate refusal as before.

- The viewer sends its session id on both calls and offers the held page's Cancel to the sessions the service lets drop it (the session in control, or the owner's); a page is held only while an agent drives, so in practice that is the owner's viewer.

Touch through neko's input driver (spec section 14):

- `xorg.conf` declares neko's `xf86-input-neko` touchscreen (`screen_touchscreen`, `Driver "neko"`, `SocketName /tmp/screen-runtime/xf86-input-neko.sock`, the layout's `CorePointer`) and `neko.yaml` enables `desktop.input` on that socket; the stack waits for the display socket and the driver's socket before starting the other members, removes a stale driver socket before Xorg starts, and reports `installing` until the env.d unit has put `/usr/lib/xorg/modules/input/neko_drv.so` in place.

- The viewer sends a phone's tap as `control/touchbegin` and `control/touchend` at the mapped point with the finger's pressure (a byte, never zero) and a fresh sixteen-bit touch id, when neko's `system/init` says `touch_events`; otherwise, and for a long press (a right click), the mouse path stays.

- `test_screen_integration.py` asserts in the workspace image that Xorg loaded the `neko` driver for the device and accepted neko's connection, that `system/init` carries `touch_events`, and that a touch over neko's WebSocket moves the core pointer to the touched point (read back with `xdotool getmouselocation`).

The pointer (spec section 12):

- The controller's own pointer takes the remote cursor's shape. neko sends the X cursor's image (size, hotspot, PNG) over the peer's WebRTC data channel when the channel opens and on every change, and the viewer sets it as the stage's CSS cursor with its hotspot (a data URI), shrunk with the drawn video when the stage shows the desktop smaller than 1:1 and capped at 128 px a side, with the browser's own pointer as the fallback when the image cannot be drawn. The stage no longer hides the pointer (`cursor: none`), so a viewer that is watching sees the default pointer, and the screencast fallback its painted-in pointer; `show_pointer` stays off in neko's WebRTC pipelines.

Sizing from a small desktop window (spec sections 7.2 and 14):

- A phone is now a phone-sized viewport with a finger as the primary pointer (the shared `PHONE_MEDIA_QUERY` and `TOUCH_MEDIA_QUERY` both), so a Screen window that is merely small on a desktop reports its stage size and the Screen resizes to fit it instead of being scaled down to a tiny 1920x1080; real phones keep the mobile controls and never resize the Screen.

- The controller's latest stage size goes out as soon as its session controls with the stack running, whether or not it matches the last report (the Screen may have been sized by someone else meanwhile).

The screencast fallback, after the first test on Imbue Cloud (spec sections 5.4 and 12):

- No pointer is painted into the screencast any more: neko's global `capture.video.show_pointer`, the flag its screencast pipeline takes (the per-pipeline flag covers the WebRTC pipelines alone), is off in `neko.yaml`, so the browser's own pointer is the one pointer there instead of two, one of them lagging. The screencast runs at 5 fps rather than 10 (each frame is a full JPEG of the display, and the fallback is for seeing the Screen, not for motion), and the viewer polls it at that rate.

- The rate lives in two places (neko's `capture.screencast.rate` in `neko_config.py`, the viewer's `SCREENCAST_FPS` in `frontend/src/screencast.ts`); a `frontend`-marked test now reads the viewer's and fails when the two drift apart.

- The viewer tries WebRTC again from the screencast instead of staying there until a reload: 15 s after the fall, then 30 s, then every 60 s, and at once on the strip's new "Retry now" button, each time with fresh `connect-info?relay=1`; the screencast keeps showing the Screen until a retry's peer connects. The strip's row carries a loud alert while the picture is the screencast ("Low-quality picture: no direct or relay path connected. Retrying.") in place of the quiet "Slow picture" line.

- A screencast retry still pending when the stream asks for a fresh peer on its own (the server dropped a retry's peer) is dropped by the new attempt, rather than firing later and restarting ICE on the peer that connected. A peer that connects late, after its deadline had already fallen the viewer back to the screencast, drops the pending retry the same way.

- While the screencast shows the Screen, a peer that fails or is set up again leaves it showing whatever the policy's phase (a late peer that fails, or one failing while a retry's relay servers are fetched, no longer flips the viewer to a blank "Connecting" until the next retry); the stream keeps that rule in one place.

- The strip gains a "Details" toggle: a panel over the stage naming the media path in use (direct or through a relay, udp or tcp, the remote address, from the peer's `getStats()`), the endpoints `connect-info` offered, the relay server count, the Screen's size and profile, and the picture's size, rate, bitrate and round trip; the viewer also logs one "media path" line to the console per connection.

- `GET /api/connect-info` waits up to eight seconds for relay servers (candidates keep their two): the desktop's first `ice-servers` answer mints a credential through the connector and Cloudflare, which took longer than two seconds on the dev tier and left the first relay attempt with no servers, which is how the whole first test ran in the screencast.

The browser shortcut on the Screen's desktop (spec section 16):

- XFCE's "Web Browser" launcher failed on a fresh Screen (`exo-open --launch WebBrowser` found no helper and `sensible-browser` no `x-www-browser`). The service now renders the session's browser entries into the home at each stack start (`xfce_helpers.py`): `~/.config/xfce4/helpers.rc` naming a `WebBrowser` helper, the helper's `.desktop` under `~/.local/share/xfce4/helpers/`, an application `.desktop` under `~/.local/share/applications/`, and `~/.config/mimeapps.list` pointing the `http` and `https` scheme handlers at it, each merged with what the file already holds (a repeated key keeps its last value, and a file the service cannot read, parse or write is logged and left alone without failing the start). All of them run `screen-browser-open`, a one-line script of the package that calls the new `screen browser open [URL]` verb, which posts to `/launch/browser`: the page opens in the fleet's one Chromium (its profile, its CDP proxy, the lease, the held-URL rule while an agent drives) rather than in a second browser. The integration suite runs the verb against a real listener and sees the tab.

The mouse wheel (spec section 11):

- Scrolling was inverted on a conventional mouse: neko's `XScroll` sends X button 4 (wheel up) for a positive `delta_y` and button 6 for a positive `delta_x`, the opposite of the DOM's sign (neko's own client flips it by default). The viewer now flips the sign once, where it sends `control/scroll` (`input.ts`), for the wheel and the phone's two-finger scroll alike; the gesture classifier reports wheel steps in the DOM's sign as before.

- Chromium starts maximized (`--start-maximized`) instead of at a fixed 1280x800: the Screen is sized to the controller's window and is often smaller than that, xfwm4 never shrinks a window larger than the display, and a maximized window follows the Screen's later size changes.

Scrolling on a phone (spec section 14):

- A one-finger drag on the video is now a touch held and moved on the Screen through neko's input driver (`control/touchbegin`, `control/touchupdate`, `control/touchend` at the mapped points), so a page in the browser scrolls under the finger with the browser's own momentum, as on any phone. Once the video is pinch-zoomed the drag pans the view as before, and without the driver it always does. The two-finger wheel scroll stays.

- The mobile bar gains Scroll Up and Scroll Down buttons, three wheel steps each, for programs on the Screen that ignore touch.

Files and the desktop's icons (spec section 4.3):

- The desktop shows no icons any more: a desktop icon wants a double click, which a phone cannot give, and anything an icon would do belongs in the panel. The service sets the `xfce4-desktop` channel's `desktop-icons/style` to none in `~/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml` before each stack start (`xfdesktop_config.py`, keeping the channel's other properties, and leaving alone a file it cannot read, parse or write).

- A dropped file still lands in the desktop folder, and the service now opens that folder in a Thunar window on the Screen right after the drop (started as `screen run` starts a command), so the file is reachable from a phone and the strip's "Saved to ..." line has a window to point at.

- A one-finger drag that starts in the letterbox band around the video, where no touch can begin, now begins its touch where the finger first reaches the video instead of retrying the start point forever; the next drag starts afresh at its own start.

- The manifest declares a `description`, so the visiting-agent primer describes the Screen rather than listing it with none; the OpenAPI document (`api_schema_path`) stays a follow-up, since the route table it needs covers forty routes.

- Stale text: the spec body now says what the implementation notes said (the 5 fps screencast with no pointer painted into it, the retries from the screencast, the one-finger drag as a touch), `data/.state/README.md` describes `screen/` in place of the browser's screenshots, and `window-bound-resources.md` names the Screen where it named the deleted browser app.

- A drop whose Thunar window cannot be opened (the launch fails) still answers the folder and the saved names: the failure is logged at warning and the files are on disk either way, where before the route answered 500 after saving them.

- `desktop_files.py` and the drop tests describe the desktop folder as the XDG desktop directory a drop shows in a file manager window, no longer as the folder `xfdesktop` shows as icons.

- `routes_test.py` opens a session that holds control through one helper (`_controlling_session`) in the drop and paste tests, in place of the same take-control preamble repeated in each.

- The `browser` launch path's start page is held while an agent drives the browser, as the README said and as `open:url` already did: `workspace-layout open https://...` (or the launcher's "Browser" row with a start page) no longer opens a tab over the agent's work; the viewer's agent overlay offers to take control and open it, or to cancel, and the page opens when the agent lets go.

- The service logs every lease transition at debug (controller, agent, human, pinned, the queue), so a lease that moved while nobody was watching can be reconstructed from the service log.

- A service restart ends the stack (spec section 9.3), and the viewer then said "The screen was stopped" with no reason: the stack now records why it ended in `data/.state/screen/stop_reason`, the next service life shows that reason until the next start, and a start wipes it.

- Only the human's asks are held while an agent drives (the launch path's start page and `open:url`): an agent's own `agentic-browser-fleet new --url` (a nameless `POST /browsers`) opens its page at once, as it did before the launch path's page was held, instead of waiting as a page offered to the human. The launch path also no longer raises Chromium over the agent's work when its page was held.

- A start that fails records its reason in `data/.state/screen/stop_reason` as a stop does, so a service restart after a failed start shows why the stack did not start rather than the reason of an earlier stop.

- `stack_test.py` builds the second service life's stack through one helper (`_second_service_life`) in the adoption and stop-reason tests, in place of the same construction repeated in each.

- An agent's `handoff` gives the Screen to the human pinned, as the browser app's lease did, rather than freeing it with the agent queued: no other agent's `acquire` is granted until the human gives it back (`busy_human`), the human's first input or Take control claims the handoff with the pin kept, and the owner may give an unclaimed handoff back from the strip (`POST /api/control/return` accepts the owner's session for it). The strip reads "Control: Handed to you" meanwhile and input flows so the first click claims it.

- The stack's programs sit at `SCREEN_STACK` (850) rather than the service's band: under memory pressure the desktop is shed after the programs on it and before any agent, and the service (still at 72) starts it again on the next open. The README's band section and spec section 9.4 say so.

- The Screen's desktop folder is `data/Desktop` under the workspace, where the Files app shows it, and it is the download folder too: the service renders the session's `~/.config/user-dirs.dirs` before each stack start with `XDG_DESKTOP_DIR` and `XDG_DOWNLOAD_DIR` both naming it (`user_dirs.py`, the file's other entries kept, `xdg-user-dirs-update` turned off), so a dropped file and a Chromium download land in one place. A drop raises the Thunar window already showing the folder instead of opening one more.

- A page asked for while Chromium is still launching (an agent's own `new --url`, or a launch path's start page) is queued for the launch and opened in front once Chromium is up, rather than parked as a page held from the human: the viewer no longer offers "Take control and open it" for a page nobody is holding back.

The Screen app (`docs/system/specs/screen/spec.md`, implementation steps 1 and 2): the workspace's one streamed Linux desktop as a window.

- The `screen-service` daemon on `127.0.0.1:8090` renders `xorg.conf`, `neko.yaml` (mode 600, with that start's passwords and API token) and the PulseAudio script under `data/.state/screen/`, starts the `screen-stack` group on demand (a page load, the Start button, the `browser` launch path, `screen run`, an agent's `acquire`) and answers `starting` until neko's health route does; `GET /api/state` carries the lifecycle (`installing` while env-converge has not installed the stack), the lease, the frontmost window's title and the display mode; `/api/ws` pushes `state`, `stopped` and `handoff_request`.

- Sessions and identity: `POST /api/session` logs the requester in to neko with the user password and a name from `X-Imbue-Identity` (the name, else the email's local part, else Owner or Guest); the viewer logs out on `pagehide` and the service deletes sessions disconnected for ten minutes.

- The control lease (`ScreenControl`), the browser fleet's state machine with `acquire`, `release`, `take_control`, `handoff` and `return_to_agents`, a FIFO wait queue and a resume queue, the 60-second idle TTL and the 12-second claim window, checkpointed to `control.json` and projected onto neko (`locked_controls` while an agent holds it, the human made host otherwise, a human taking an uncontrolled Screen learned from `control/host`). Human routes `POST /api/control/take` and `/return`; agent routes `/acquire`, `/release`, `/handoff` with `X-Mngr-Agent-Id`.

- `GET /api/screenshot` (PNG by default, from neko's admin screenshot), `GET /api/windows` and `POST /api/windows/<id>/activate` through `wmctrl`, `POST /api/run` launching a command with `DISPLAY=:99` in the `AGENT_SUBPROCESS` band, the `/neko/` reverse proxy (HTTP and WebSocket, with a rate limit on the login route), and `GET /api/connect-info` answering no endpoints yet.

- The `screen` command: `run`, `windows`, `activate`, `shot`, `size` (read only for now), `control acquire|release|handoff`, `open`, with the fleet CLI's exit codes. The `screen-watch` event listener stops the rest of the group when a member exits, appends a `screen_stopped` event and tells the service.

- The viewer (`frontend/`, built into `src/screen/static/`): the video over neko's signaling with the server's candidate used as given, the strip (who has control, Take control, Give control back), the installing, starting and stopped overlays, the agent dim, mouse and keyboard input while this session controls, and the shell connection with `closeChord` sending Ctrl+W to the remote.

Implementation step 3 (sizing, profiles, visibility, audio):

- `POST /api/screen-size` resizes the Screen from the controller's stage size: rounded down to 16 px (the cap itself at or past 1920 or 1080), clamped to 640x480 through 1920x1080, the RANDR mode `<w>x<h>_60` added with `xrandr --newmode`/`--addmode` from the package's own CVT modeline when the dummy output lacks it, then selected through neko. A non-controller's report is ignored; added modes are recorded in `data/.state/screen/modes.json` for the stack's life. `screen size WxH` sets it through the same route.

- The profile chooser: `control` while a human holds control and sent input in the last ten seconds, `idle` after a minute without input while no agent holds the lease, `watch` otherwise, fed by `POST /api/input-seen` from the controller's viewer and by every agent command; announced as `profile` on `/api/ws` and in `GET /api/state`, and selected by every viewer with `signal/video`.

- The viewer releases its media (`signal/close`) after five seconds hidden, judged from `document.hidden`, the shell's `shown`/`hidden` messages and an IntersectionObserver on a 1.5-second belt, and requests it again when visible; input goes only while visible and controlling. A phone never reports a size. A large mute control in the strip enables audio for this session alone (`signal/audio`); every session starts with audio disabled.

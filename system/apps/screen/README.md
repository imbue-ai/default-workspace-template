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

- `GET /`: the viewer page (the frontend's build, `src/screen/static/index.html`; a
  placeholder until it is built), with its bundle under `/assets/`. A page load starts
  the stack when it is not running.
- `GET /api/state`: `{"lifecycle", "install_reason", "stop_reason", "title", "video_id",
  "control", "screen_size", "chromium"}`. `lifecycle` is `installing` while the env.d
  unit has not installed the stack, `starting` while the group comes up, `running`,
  or `stopped` with the `stop_reason`. `title` follows the frontmost X window.
- `GET /api/ws`: the service's own events to viewers: `state` (the document above, on
  connect and on every change), `stopped` with a reason, `handoff_request` when an
  agent hands the Screen to the human.
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
- `GET /api/connect-info`: `{"ice_servers", "candidates", "expires_at"}`; empty until
  the desktop's brokers land, which the viewer reads as "use the server's own candidate".
- `POST /launch/browser`: the `browser` launch path; it starts the stack and answers
  `{"path": "/"}` (Chromium itself arrives with the browser's migration).
- `/neko/...`: the reverse proxy to neko's HTTP and WebSocket on `127.0.0.1:8091`, with
  a rate limit on `/neko/api/login`.
- `GET /_static/app_contract.js` and `/_static/context_menu.js`: the shell's modules,
  served from this origin as every app serves them.
- `GET /health`: `{"status", "lifecycle"}`.

## Pointer lock

The viewer carries the pointer-lock plumbing of spec section 12 (`frontend/src/pointer_lock.ts`:
a strip control that locks the pointer on the stage, the relative motion it yields while locked,
Escape releasing it in the browser itself), but the control is hidden: neko v3 has no
relative-motion event. Its `control/move` carries an absolute `{x, y}` only
(`server/pkg/types/message/messages.go` defines no delta message for pointer motion and
`server/internal/websocket/handler/control.go` no handler for one; the only deltas are the
scroll's), and integrating deltas into absolute moves breaks as soon as a relative-motion program
warps the pointer itself, which is what such programs do. `IS_RELATIVE_MOTION_SUPPORTED` turns
the control on once neko gains such an event.

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
API token), `pulse.pa`, `control.json`, `events/screen/events.jsonl` (the
`screen_stopped` events the watcher appends).

## The command

`uv run --no-sync screen` is the agent-facing command (spec section 17): `run`,
`windows`, `activate`, `shot`, `size`, `control acquire|release|handoff`, `open`. Its
exit codes are the fleet CLI's: 0 ok, 1 error, 2 preempted (a human has or took
control; end the turn), 3 busy, 4 timed out, 64 usage, 69 no service.

## Packaging

The env.d unit `system/scripts/env.d/2000-screen.sh` installs the stack's apt packages
from the pinned snapshot and the pinned neko binary at `/opt/neko/bin/neko` (and
Fortress, which moved there from the browser's unit). Until it is satisfied the service
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

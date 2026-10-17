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
  `{"session_id", "token", "name"}`; the viewer uses the token under `/neko/`.
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
- `POST /launch/browser`: the `browser` launch path; it starts the stack and answers
  `{"path": "/"}` (Chromium itself arrives with the browser's migration).
- `/neko/...`: the reverse proxy to neko's HTTP and WebSocket on `127.0.0.1:8091`, with
  a rate limit on `/neko/api/login`.
- `GET /_static/app_contract.js` and `/_static/context_menu.js`: the shell's modules,
  served from this origin as every app serves them.
- `GET /health`: `{"status", "lifecycle"}`.

## Connectivity

The stream is ICE over neko's UDP and TCP muxes on port 59000 inside the container (spec
section 5). Every create template publishes that port (`publish__extend = ["59000/udp",
"59000/tcp"]`), so the host reaches it at a host port the provider picked; a shared
workspace's visitors reach it through the relay's `screen-media` tcp proxy or through TURN.
Neko itself is told nothing about any of this: its candidates carry the container's own
address, and the viewer rewrites them.

`GET /api/connect-info` asks two brokers in parallel, each bounded to two seconds, and
answers the union:

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
screencast (`capture.screencast`, 10 fps at quality 60, polled at
`/neko/api/room/screen/cast.jpg`) with input still over the WebSocket, and the strip says
"Slow picture: no media path connected".

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

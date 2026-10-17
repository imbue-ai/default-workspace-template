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
  or `stopped` with the `stop_reason`. `title` follows the frontmost X window;
  `video_id` is the profile every viewer selects (below).
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
- `GET /api/connect-info`: `{"ice_servers", "candidates", "expires_at"}`; empty until
  the desktop's brokers land, which the viewer reads as "use the server's own candidate".
- `POST /launch/browser`: the `browser` launch path; it starts the stack and answers
  `{"path": "/"}` (Chromium itself arrives with the browser's migration).
- `/neko/...`: the reverse proxy to neko's HTTP and WebSocket on `127.0.0.1:8091`, with
  a rate limit on `/neko/api/login`.
- `GET /_static/app_contract.js` and `/_static/context_menu.js`: the shell's modules,
  served from this origin as every app serves them.
- `GET /health`: `{"status", "lifecycle"}`.

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
video element. In neko v3.2.0 the audio track is attached to the audio pipeline when
the peer is created and only paused while disabled, so the audio pipeline (pulsesrc on
the null sink's monitor into opusenc) runs whenever any peer exists, whatever the
sessions' audio flags; its device is already the null sink's monitor, so the spec's
fallback is in place by construction and what remains is the encoder's cost on silence,
measured below.

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

## The command

`uv run --no-sync screen` is the agent-facing command (spec section 17): `run`,
`windows`, `activate`, `shot`, `size [WxH]` (reads the size, or sets it through
`POST /api/screen-size` as the controller's viewer would), `control
acquire|release|handoff`, `open`. Its
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

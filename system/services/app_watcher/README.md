# app_watcher

Background service that watches the app registry (`data/.state/apps.toml`).
On startup and on every change it writes `service_registered` /
`service_deregistered` events to
`$MNGR_AGENT_STATE_DIR/events/services/events.jsonl` so the minds desktop
client can discover which app ports an agent is exposing. A registration
event goes out only for an app whose registered fields (URL, label, icon)
changed, since the whole registry is rewritten whenever any app registers;
the first pass after startup remembers nothing and announces every app, which
is what a consumer reading the stream from its start needs.

Uses inotify when available on Linux, and falls back to mtime polling
(5-second interval) otherwise -- under gVisor and on the lima/vps providers,
changes made outside the sandbox raise no in-sandbox inotify events, so
polling bounds the worst-case discovery latency.

(The terminal app separately writes a `server_registered` event to
`events/servers/events.jsonl` when it starts; that is a different,
hand-written stream, not this service.)

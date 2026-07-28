# app_watcher

Background service that watches the app registry (`data/.state/apps.toml`).
On startup and on every change it writes `service_registered` /
`service_deregistered` events to
`$MNGR_AGENT_STATE_DIR/events/services/events.jsonl` so the minds desktop
client can discover which app ports an agent is exposing.

Uses inotify when available on Linux, and falls back to mtime polling
(5-second interval) otherwise -- under gVisor and on the lima/vps providers,
changes made outside the sandbox raise no in-sandbox inotify events, so
polling bounds the worst-case discovery latency.

(The terminal's `run_ttyd.sh` separately writes a `server_registered` event
to `events/servers/events.jsonl`; that is a different, hand-written stream,
not this service.)

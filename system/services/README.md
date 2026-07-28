# system/services/

Background services: supervised programs with no tab of their own. They keep
the workspace running -- backing it up, keeping tunnels alive, watching state
-- without the user ever needing to open them.

- `app_watcher/` - Watches the app registry (`data/.state/apps.toml`), writes
  server events for discovery, and reconciles with the Cloudflare forwarding
  API.
- `cloudflare_tunnel/` - Runs the Cloudflare tunnel for global access when a
  tunnel token is present.
- `host_backup/` - Continuous restic backup of the whole host directory to a
  remote repository.
- `env_converge/` - One-shot environment convergence on boot (deferred
  installs at the pinned apt snapshot).
- `oom_priority/` - The OOM-prevention machinery: priority bands, the shed
  ledger, and the earlyoom integration.

Each is a uv workspace member (the `system/services/*` glob) with its own
README. A background service that exists solely to support one app does NOT
go here -- it lives in that app's folder under `system/apps/` and is named
`<app>-<role>` in `system/supervisord.conf`.

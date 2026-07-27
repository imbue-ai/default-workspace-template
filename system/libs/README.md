# system/libs/

The built-in workspace packages. Each is a uv workspace member with its own
`pyproject.toml`; see each package's README for details.

Services (supervised via `system/supervisord.conf`): `system_interface` (the
workspace web UI), `app_watcher`, `browser`, `cloudflare_tunnel`,
`github_sync`, `host_backup`, and the one-shot `env_converge`. Libraries:
`bootstrap` (first-boot setup, then launches supervisord), `oom_priority`,
`tk_command_parsing`, `mngr_cli_contract`.

User-built packages do NOT go here -- they live in `creations/` at the
workspace root (see the build-web-service skill).

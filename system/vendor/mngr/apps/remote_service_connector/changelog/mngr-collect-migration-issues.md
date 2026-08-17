The `/account`, plan-switch, and admin account responses temporarily serve the
Cloudflare-tunnel-era fields v0.3.11 desktop clients require with no defaults
(`max_tunnels` / `max_services_per_tunnel` in entitlements, `tunnels` in
usage) as hardcoded zeros. Migration 020 dropped those columns, so without
this the first connector deploy would break every v0.3.11 client's Accounts
page with a validation error. Marked `# CLEANUP:` for removal once the
desktop fleet is on the first post-v0.3.11 release.

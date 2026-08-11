Added the `minds-web-client` blueprint (`blueprint/minds-web-client/plan-minds-web-client.md`): the implementation plan for a hosted, browser-only minds client at minds.imbue.com.

The plan covers the browser-orchestrated create flow over a new connector claim endpoint, workspace access via the existing share stack with an owner fast path, a new in-workspace `owner-exec` service authenticated by Ed25519 signatures against `authorized_keys`, DEK-in-browser sync-record writes, in-workspace backup provisioning, and the desktop-side auto-share toggle and grants single-writer migration.

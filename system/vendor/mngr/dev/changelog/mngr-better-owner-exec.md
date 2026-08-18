- Added the `blueprint/owner-exec-vm` design plan and implemented the
  `owner-exec` service: a standalone Go daemon (new `imbue-ai/owner-exec` repo)
  giving the workspace owner SSH-equivalent authority over HTTP, signed per an
  RFC 9421/9530 strict profile with Ed25519 keys verified against
  `authorized_keys`. It replaces the in-container Python owner-exec service and
  adds a VM-level instance on remote outer hosts (imbue-cloud slices and VPS),
  so web-only workspaces can configure latchkey and other components that run
  outside the container without SSH.

- Added the `bump-owner-exec` skill documenting the two version pins (monorepo
  VM install + default-workspace-template in-container install) and the two
  vendored copies of the shared crypto vectors.

- Pin `ssh_bind_address = "127.0.0.1"` in the `[providers.docker]` block, so
  every local-Docker workspace container publishes its sshd on loopback only
  and is not reachable from the machine's LAN. This matches mngr's new default
  for a local daemon (imbue-ai/mngr-internal#973) and pins it so the
  workspace's exposure does not depend on that default. Requires a vendored
  mngr that knows the field: an older `system/vendor/mngr` rejects it as an
  unknown provider setting, so this lands only after the next vendor sync.
  Existing containers keep the bind they were created with until they are
  recreated or restored from a snapshot.

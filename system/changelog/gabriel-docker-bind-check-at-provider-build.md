- Drop the `ssh_bind_address = "127.0.0.1"` pin from the `[providers.docker]`
  block. With a user config that points docker at a remote daemon
  (`host = "ssh://..."`), the pin produced a loopback bind mngr cannot reach, so
  docker workspace creation failed; with an mngr pin older than
  imbue-ai/mngr-internal's matching fix, every `mngr create` run from the
  template (imbue_cloud included) failed while loading config. Local-daemon
  containers still publish sshd on loopback only, since that is mngr's default.

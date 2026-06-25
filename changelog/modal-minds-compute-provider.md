- Added a `[create_templates.modal]` block so the minds app can launch a
  workspace on a Modal sandbox (the "modal" compute provider). Like the lima
  template, Modal has no Dockerfile build, so the toolchain is provisioned over
  SSH after the sandbox boots by reusing the same `setup_system.sh` /
  `install_dependencies.sh` / `build_workspace.sh` scripts a Dockerfile-built
  workspace runs. It sets `provider = "modal"`, flips the default-disabled
  modal provider on for the create (`providers.modal.is_enabled=true`), forwards
  the Anthropic creds + `GH_TOKEN`, and sets `idle_mode = "disabled"`. There is
  intentionally no autostart unit (the lima-autostart step is omitted): Modal
  sandboxes are ephemeral (~1 day) and do not survive a reboot, so the minds
  desktop app re-creates them rather than relying on systemd.

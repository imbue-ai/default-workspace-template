- Fixed agent creation on the Lima and Modal providers, which broke when the
  browser CDP handover reached `main` with `setup_system.sh: line 269:
  PLAYWRIGHT_CLI_VERSION: unbound variable`. The `@playwright/cli` install was
  added to `system/scripts/setup_system.sh` but its version was pinned only as a
  `system/Dockerfile` `ARG`, so it existed for docker-built images and was
  undefined everywhere the script runs directly -- and the script runs under
  `set -u`. `setup_system.sh` now carries its own
  `: "${PLAYWRIGHT_CLI_VERSION:=0.1.18}"` default like every other pinned tool,
  and installs `@playwright/cli` in its own stanza with the `command -v` check
  the neighbouring agent CLIs have, rather than inside the Pi CLI's.

- Added `system/toolchain_pins_test.py`, which fails if `setup_system.sh`
  expands a `${..._VERSION}` it does not also default. CI builds the image but
  never provisions a Lima or Modal VM, so nothing else catches a pin that leaves
  the non-docker providers broken.

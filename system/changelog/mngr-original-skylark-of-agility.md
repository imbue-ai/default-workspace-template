- Fixed agent creation on the Lima and Modal providers, which had been failing
  since the browser CDP handover landed with `setup_system.sh: line 269:
  PLAYWRIGHT_CLI_VERSION: unbound variable`. The `@playwright/cli` install was
  added to `system/scripts/setup_system.sh` but its version was pinned only as a
  `system/Dockerfile` `ARG`, so it existed for docker-built images and was
  undefined everywhere the script runs directly -- and the script runs under
  `set -u`. `setup_system.sh` now carries its own
  `: "${PLAYWRIGHT_CLI_VERSION:=0.1.18}"` default like every other pinned tool.

- Added `system/toolchain_pins_test.py`, which fails if any `ARG <NAME>_VERSION`
  in `system/Dockerfile` lacks a matching `: "${<NAME>_VERSION:=...}"` default in
  `setup_system.sh`, or if the two disagree. CI only builds the docker image, so
  nothing else catches a pin that leaves non-docker providers broken.

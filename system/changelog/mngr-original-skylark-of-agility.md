- Fixed agent creation on the Lima and Modal providers, which aborted with
  `setup_system.sh: PLAYWRIGHT_CLI_VERSION: unbound variable`. `@playwright/cli`
  was pinned only as a `system/Dockerfile` `ARG`, so it was defined for image
  builds and unset everywhere the script runs directly -- under `set -u`.
  `setup_system.sh` now carries its own `: "${PLAYWRIGHT_CLI_VERSION:=0.1.18}"`
  default, like every other pinned tool.

- Added `system/toolchain_pins_test.py`: fails if `setup_system.sh` expands a
  `${..._VERSION}` it does not also default. CI never runs that script, so
  nothing else catches a pin that leaves the non-docker providers broken.

- The terminal app depends on `imbue-mngr-ttyd` (at the mngr commit `pyproject.toml`
  pins) and reads the OSC 52-capable ttyd web client from that package's resources
  instead of a vendored mngr tree; `--ttyd-web-client` is now an optional override.

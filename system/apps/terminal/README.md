# terminal

The terminal tab: a web terminal served by [ttyd](https://github.com/tsl0922/ttyd),
supervised as the `terminal` program in `system/supervisord.conf`.

`run_ttyd.sh` registers the fixed port (7681) via
`system/scripts/forward_port.py`, writes the discovery event, installs the
dispatch scripts for the different terminal flavors (attach to an agent's tmux
window, open a shell in a directory, named persistent `terminal-N` sessions
backing the UI's "New terminal" tabs), and then execs ttyd.

The ttyd binary and its OSC 52-capable web client come from the vendored
`mngr_ttyd` plugin at `system/vendor/mngr/libs/mngr_ttyd/` -- this folder is
the template-side wiring around them.

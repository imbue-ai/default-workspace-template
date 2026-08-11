`workspace.txt` warm list: `xvfb` moved from the env.d browser-unit section to
the `setup_system.sh` section and `xclip` added alongside it, matching the
default-workspace-template change that bakes both into the base image instead
of deferring them to the env.d browser unit.

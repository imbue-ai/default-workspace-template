# When a command fails for a reason outside your code

Three failures come from the workspace itself, not from what you ran. Each section says what to check and what to do instead of retrying as is.

## Contents

- A command killed by memory pressure
- A tool that cannot run under gVisor
- A `ModuleNotFoundError` from a command this repo defines

## A command killed by memory pressure

A background OOM-prevention daemon (earlyoom) kills ("sheds") memory-heavy processes under sustained memory pressure, most expendable first: an agent's build, test and browser subprocesses before the agent itself. If a command of yours died with exit 137 (or SIGKILL/SIGTERM) and you did not kill it, confirm by looking for a record naming it (by pid or process name) in the shed ledger, `/home/user/workspace/data/.state/oom_priority/events/shed.jsonl`.

If it was shed, do NOT blindly re-run a memory-heavy command: it will likely be shed again. Find a lower-memory approach (smaller batches, streaming, releasing data you no longer need), and retry only if you can.

That is for your own ad-hoc commands. A shed command of a worker's test gate follows the gate's own shed handling, in the worker's guide, and memory pressure is never a reason to skip one.

## A tool that cannot run under gVisor

Some providers run the workspace container under gVisor (`runsc`), a user-space kernel between the container and the host kernel. Check with `dmesg 2>/dev/null | grep -q 'Starting gVisor'` (`uname -r` also reports `4.19.0-gvisor`).

Most software is unaffected, but these do not work inside the sandbox: ptrace-based tooling (`strace`, `gdb` attach, `perf`), eBPF, FUSE mounts, `io_uring`, nested container runtimes (docker or podman inside the workspace), and unusual `ioctl`s. Filesystem-metadata-heavy operations (`find`, `tar`, `git status` over large trees) are several times slower than on a plain kernel, and interpreter startup is somewhat slower.

Do not try to install or "fix" any of these. Work around them (`--no-sandbox` for Chromium, logging instead of `strace`), and tell the user when a tool is unavailable for this reason.

## A `ModuleNotFoundError` from a command this repo defines

A third-party module missing from a command this repo defines (like `mngr`) is fixed by refreshing the install you ran, and a standard workspace has two:

- `uv run --no-sync mngr ...` resolves from the root venv: run `uv sync --all-packages`.
- A bare `mngr` on PATH is the uv-managed tool `system/scripts/build_workspace.sh` installs at build time: run `python3 system/scripts/install_mngr.py`, the same program the build runs. It reinstalls the tool with the plugins `system/config/mngr_plugins.toml` assigns it, into the pinned tool directory the build installs into and puts first on `PATH`. If `mngr` on your PATH then still comes from somewhere else (a workspace created before that pin has a second copy under `/home/user/.local`), run `python3 system/scripts/workspace_bare_scripts/tool_env.py drop-shadowing-mngr`, which is what the build runs next and what leaves the refreshed install the one a login shell reaches.

Do not hand-roll the `uv tool install`: installing under the wrong `$HOME`, or with no plugins, leaves an mngr that cannot parse `[agent_types.*]`, which is what `install_mngr.py` exists to prevent. `uv tool list` shows what is installed. Then run the command again.

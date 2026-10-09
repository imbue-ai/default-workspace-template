# Running Python in this workspace

How to start any Python program here -- a one-off command, a script, or a
long-running service -- and why. The short version:

- **Run scripts and other one-offs with `uv run --no-sync`**, unless the
  instruction you are following says `python3`.
- **Never run a long-running process under `uv run`.**

## One-offs: `uv run --no-sync`

A one-off is anything you start and wait for: a CLI (`mngr`, `app-manifest`,
`tk`, `pytest`, `ruff`), a skill's script, `python -c ...`. Run it from the
repo root as `uv run --no-sync <command>`.

A plain `uv run` first checks `uv.lock` against every `pyproject.toml` and
syncs the venv to it. That costs time on every call (roughly 100-140 ms on
top of `--no-sync`, measured under gVisor), and when the lock has drifted it
rewrites `uv.lock` in the middle of whatever you were doing. `--no-sync` skips
both and leaves about 40 ms of uv over running the entry point directly.

The venv stays current without those per-call syncs:

- each session starts with a `--frozen` sync
  (`system/scripts/session_start_sync.sh`), which also regenerates a drifted
  lock once and says so;
- after you change a dependency yourself, run `uv lock` and then
  `uv sync --all-packages`, and commit the new `uv.lock` with the change.

`--no-sync` is a no-op for a script carrying inline (PEP 723) metadata: uv
always runs those in their own environment.

## When the instruction says `python3`

Some scripts must run with the system `python3` and no venv at all -- hook
scripts, the bug-report collector, steps that run before the venv exists, and
scripts that other programs call by path. Where a doc, a config file or a
skill tells you to run something with `python3`, do exactly that. Everywhere
else, use `uv run --no-sync`: a stdlib-only script runs fine under it, while
`python3` on a script that needs the venv fails at its first third-party
import.

## Long-running processes: sync, then `exec` the entry point

A supervisord program (or anything else that stays up) runs its entry point
directly from the venv:

```ini
command=python3 system/services/oom_priority/bin/oom_tag_service.py user bash -c "uv sync --all-packages --frozen && exec .venv/bin/my-service"
```

Under `uv run`, a `uv` process would stay resident as the program's parent
for its whole life (about 19 MB each), sit between supervisord and the
program, and could relock the workspace on every restart. The `--frozen`
sync makes sure the entry point exists and matches the committed lock without
ever rewriting it. An app with an `app.toml` runs its own tool's entry point
instead (see `service-processes.md`).

## Keep startup imports light

A one-off pays for its imports on every call, and on gVisor that dominates:
importing pydantic and building models took about 450-560 ms of a CLI's
startup, against the 40 ms that uv adds. So in a script or CLI entry point,
import only what every invocation needs at the top of the module, and import
heavy libraries (pydantic, loguru, click, httpx, ...) inside the function or
subcommand that uses them.

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

- the workspace syncs `--frozen` from the committed lock at boot, and a
  worker's checkout at provisioning;
- a claude or codex session starts with the same sync
  (`system/scripts/session_start_sync.sh`), which never rewrites `uv.lock`: if
  the lock has drifted from `pyproject.toml` it says so once and syncs from
  the lock as committed (other harnesses rely on the boot sync);
- after a merge or pull that changes `uv.lock` (a worker's branch that added a
  dependency, say), run `uv sync --all-packages --frozen` before the next
  one-off;
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

## The two tiers, and how scripts are laid out

Every Python script here is a module of a package. A skill keeps its code in
its own uv project, `python/` (`python/pyproject.toml`, package
`python/<skill_name>_skill/`), and its entry files (and any shell scripts) in
`scripts/`. `.agents/shared/scripts` (package `agents_shared`) and
`system/scripts` are a project each, with the packages beside the entry files.
A built-in script that only this release calls has no entry file: its module
has its own `if __name__ == "__main__":` block and runs by its path
(`uv run --no-sync system/scripts/workspace_scripts/smoketest_app.py`). Such a
run puts the module's own directory first on `sys.path`, so a package with one
never holds a module named like a standard-library module. An entry file is
kept where a script runs with no venv or something outside this release calls
it by a fixed path. The code runs under one of two interpreters:

- **Venv tier** (the default): runs from the root venv, through
  `uv run --no-sync <entry>`. It may use any dependency its `pyproject.toml`
  declares. Every skill script you write belongs here.
- **Bare tier**: runs under the system `python3` with no venv, so it imports
  only the standard library. It exists for code that must work when the venv
  is missing or broken, that something outside this release calls by path, or
  that sits in a long-running program's launch chain: the agent hooks, the
  bug-report collector, the pre-sync build steps, the scripts the Imbue Studio
  app calls, update-self, and `system/scripts/workspace_bare_scripts/`
  (`forward_port.py`, `with_secrets.py`, `run_in_background.py`, ...).

You never need to know which tier a script is in: follow the rule at the top
(`uv run --no-sync`, unless the instruction says `python3`).

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
subcommand that uses them. A test holds every entry point to this; an entry that
really needs one at import declares it in its project's `pyproject.toml` under
`[tool.workspace-template.entry-points."<entry>.py"]` as `heavy-imports`.

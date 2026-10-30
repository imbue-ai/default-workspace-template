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

Every Python script here is a module of a package, with one exception below. A
skill keeps its code in its own uv project, `python/` (`python/pyproject.toml`,
package `python/<skill_name>_skill/`), and any shell scripts in `scripts/`.
`.agents/shared/scripts` (package `agents_shared`) and `system/scripts` are a
project each. A script's module has its own `if __name__ == "__main__":` block
and runs by its path
(`uv run --no-sync system/scripts/workspace_scripts/verify_app.py`). Such a
run puts the module's own directory first on `sys.path`, so a package with one
never holds a module named like a standard-library module.

An entry file (a stub at the top of a `scripts/` dir) is kept only where
something outside this release runs a fixed path, where a script that runs
with no venv imports other modules of its package, or where a script must put
a directory on `sys.path`; each stub says which. A stub run by the system
`python3` finds its package only because Python puts the stub's own directory
first on `sys.path`, so it is run as `python3 <path>`, never with `-I`, `-P`
or `PYTHONSAFEPATH` set.

The code runs under one of two interpreters, and what it imports decides which:

- **Bare tier**: code whose imports are all standard library -- the bare
  packages (`workspace_bare_scripts`, update-self's) count as standard library
  here -- runs under the system `python3` with no venv. It pays nothing for an
  environment it does not use. This also covers code that must work when the
  venv is missing or broken, that something outside this release calls by
  path, or that sits in a long-running program's launch chain: the agent
  hooks, the bug-report collector, the pre-sync build steps, the scripts the
  Imbue Studio app calls, update-self, `system/scripts/workspace_bare_scripts/`
  (`forward_port.py`, `with_secrets.py`, `run_in_background.py`, ...), and
  stdlib-only modules of other packages such as `notify_user.py` and
  `request_secret.py`.
- **Venv tier**: code that imports a third-party package, or a venv-tier
  package of this workspace, runs from the root venv through
  `uv run --no-sync <entry>`. It may use any dependency its `pyproject.toml`
  declares.

A bare module that imports a bare package it does not sit beside puts that
package's directory on `sys.path` itself, only when it runs by its path
(`if not __package__:`), as path arithmetic on `Path(__file__)`. A test holds
every such edit to the directories the bare type check searches, so what
the checker resolves is what the runtime imports.

A skill whose Python is one standard-library-only script is the exception to
the package layout: a **bare skill** keeps it as `scripts/<skill_name>.py`
(underscores for hyphens), optionally with its test beside it as
`scripts/<skill_name>_test.py`, and no `python/` project. It runs as
`python3 .agents/skills/<name>/scripts/<skill_name>.py`, and is type-checked
like the rest. A skill that needs a dependency, or a second module, uses the
`python/` project instead.

You never need to know which tier a script is in: follow the rule at the top
(`uv run --no-sync`, unless the instruction says `python3`).

## Calling workspace code from Python

Code that needs another part of the workspace imports it, declaring its
package in its own `pyproject.toml`. It does not start one of the workspace's
own scripts, modules or commands as a process (`python3 <path>.py`,
`sys.executable` and a path, `-m <module>`, `uv run --no-sync <command>`) to
get what an import gives: each run costs an interpreter start, and its
arguments and results become command-line words and printed text that no type
check reads.

Start a process only when the call needs one of its own:

- the code is the other tier's: bare code calling venv-tier code, or a step
  that must still run when the venv is missing or broken;
- the work must outlive its caller (a detached run, a rollback that restarts
  the caller), or be cut off at a deadline without taking the caller with it;
- the code to run is another tree's: the target release's staged copy, or the
  checkout's current one where an import would load the copy the caller
  started with;
- the process is the point: a wrapper that execs the command it is given, or
  a server.

A test counts the places that name one of the workspace's own programs to
run, and the count only goes down.

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
subcommand that uses them. A test holds every entry point to this, with no
exceptions. When code whose signatures name a heavy library's types serves only
some subcommands, give it a module of its own and import that module inside
those subcommands.

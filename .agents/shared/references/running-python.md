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
  the lock as committed. Other harnesses have no session-start hook, so on
  them run `uv sync --all-packages --frozen` yourself when a `pyproject.toml`
  or `uv.lock` changed since the workspace booted;
- after a merge or pull that changes a `pyproject.toml` or `uv.lock` (a
  worker's branch that added a skill or a dependency, say), run
  `uv sync --all-packages --frozen` before the next one-off;
- after you change a dependency yourself, run `uv lock` and then
  `uv sync --all-packages`, and commit the new `uv.lock` with the change.

`.venv` is a symlink to a directory beside the uv cache, so uv hardlinks
packages instead of copying them. Leave it to the workspace: never replace it
with a real directory, delete its target, or point it somewhere else. If a sync
fails with "failed to create directory `.venv`: File exists", the link's target
is gone; `python3 system/scripts/workspace_bare_scripts/venv_links.py ensure .`
recreates it, and the sync then refills it.

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

## Long-running processes: sync, then `exec` the entry point

A supervisord program (or anything else that stays up) runs its entry point
directly from a venv, never under `uv run`. A built-in one runs from the root
venv:

```ini
command=python3 system/services/oom_priority/bin/oom_tag_service.py system-services bash -c "uv sync --all-packages --frozen && exec .venv/bin/some-built-in-service"
```

Under `uv run`, a `uv` process would stay resident as the program's parent
for its whole life (about 19 MB each), sit between supervisord and the
program, and could relock the workspace on every restart. The `--frozen`
sync makes sure the entry point exists and matches the committed lock without
ever rewriting it.

## User packages are standalone projects

Every Python package the user's agents create under `system/apps`,
`system/services` or `system/libs` is its own uv project: its own
`pyproject.toml`, `uv.lock` and `.venv`, and not a member of the root
workspace (the root `pyproject.toml`'s `members` names only the built-in
packages). So a dependency one of them adds never moves the versions the chat,
the shell or another user package run on, and two user packages can pin
different versions of the same library. User skills stay in the shared
workspace. The rules:

- **Change its dependencies in its own directory:** `uv add <package>` (or
  `uv lock` after a hand edit, then `uv sync`) in `system/<kind>/<package>/`.
  Never `uv sync --all-packages` for it, and never add it to the root
  `members`.
- **Depend on shared code by editable path** (`app-schema`,
  `workspace-layout`, a built-in app's package):
  `[tool.uv.sources] app-schema = { path = "../../libs/app_schema", editable = true }`.
  The library's own workspace sources then apply in the package's lock, so
  `imbue-common` and the mngr packages come from the root's mngr pin; never
  write a source for `imbue-common` yourself (a second source for one package
  fails to lock).
- **Its program syncs and execs its own venv at start:** link its venv, sync
  its own project with `--locked`, then exec a script from its own `.venv/bin`:

  ```ini
  command=python3 system/services/oom_priority/bin/oom_tag_service.py user bash -c "python3 system/scripts/workspace_bare_scripts/venv_links.py ensure-project system/services/my_service && uv sync --locked --project system/services/my_service && exec system/services/my_service/.venv/bin/my-service"
  ```

  A stale lock or a failed sync fails that program's start and nothing else.
- **A one-off of its command** runs from its project:
  `uv run --no-sync --project system/<kind>/<package> <command>`. Its tests run
  the same way from its directory (`uv run --no-sync pytest`), against its own
  venv, which holds its `dev` group.
- `system/test_user_packages_are_standalone.py` checks every user package
  against these rules and its lock with `uv lock --check`.

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

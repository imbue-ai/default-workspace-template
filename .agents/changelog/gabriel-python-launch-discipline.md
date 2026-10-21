- Skill and reference commands run one-offs as `uv run --no-sync ...`, and the new `.agents/shared/references/running-python.md` gives the rule: one-offs use `uv run --no-sync` unless the instruction says `python3`, a long-running process never runs under `uv run`, and a script's top-level imports stay light.

- The github-sync service's program line and the service-processes example run the entry point from `.venv/bin` after a `--frozen` sync instead of under `uv run`.

- update-self advances the environment snapshot with `uv run --no-sync env-converge upgrade` after an apply, and the build-app scaffold validates a new manifest with `uv run --no-sync app-manifest`.

- `running-python.md` describes where the venv is kept current: a `--frozen` sync at boot, at worker provisioning, and at claude and codex session start, none of which rewrites `uv.lock`.

- update-app's preview launches the app from the worktree the way a supervisord program runs: a `--frozen` sync of the worktree's environment, then the app exec'd from its `.venv`, instead of under `uv run`, so the preview never relocks the worktree and no uv process stays resident as its parent. update-app's docs say to run `uv lock` in the worktree before a `refresh` after a dependency change.

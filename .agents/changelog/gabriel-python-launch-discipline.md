- Skill and reference commands run one-offs as `uv run --no-sync ...`, and the new `.agents/shared/references/running-python.md` gives the rule: one-offs use `uv run --no-sync` unless the instruction says `python3`, a long-running process never runs under `uv run`, and a script's top-level imports stay light.

- The github-sync service's program line and the service-processes example run the entry point from `.venv/bin` after a `--frozen` sync instead of under `uv run`.

- update-self advances the environment snapshot with `uv run --no-sync env-converge upgrade` after an apply, and the build-app scaffold validates a new manifest with `uv run --no-sync app-manifest`.

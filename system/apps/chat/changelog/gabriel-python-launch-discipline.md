The tool-call reader treats `uv run --no-sync` / `--frozen` / `--quiet` in front of `tk` the same as a bare `uv run`, so a tk step command run that way is still recognised (the option set is shared with the secrets guard through `tk_command_parsing`); the README runs the backend from `.venv/bin` and the tests with `uv run --no-sync`, and the editor format command uses `uv run --no-sync`.

The venv sync a failed chat create runs before its retry is `uv sync --all-packages --frozen`, so it never rewrites a `uv.lock` another agent has left out of date with `pyproject.toml`.

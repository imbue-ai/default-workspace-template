The opt-in github-sync service runs `.venv/bin/github-sync run` after a `--frozen` sync instead of under `uv run`; the README documents the one-shot subcommands as `uv run --no-sync github-sync ...`.

The project requires Python 3.12 or newer (was 3.11), matching the workspace floor: every workspace already runs 3.12, and the lock already required it.

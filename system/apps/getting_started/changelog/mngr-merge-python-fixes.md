The README runs the backend from `.venv/bin` and the tests with `uv run --no-sync`, and the editor format command uses `uv run --no-sync`.

The README starts the backend from `.venv/bin/getting-started` rather than under `uv run`, following the rule that a long-running process never runs under `uv run`.

The README now says the app runs from the workspace venv (its one install), not from its own uv tool.

The README runs the terminal tests with `uv run --no-sync pytest`.

The project requires Python 3.12 or newer (was 3.11), matching the workspace floor: every workspace already runs 3.12, and the lock already required it.

The README now says the app runs from the workspace venv (its one install), not from its own uv tool.

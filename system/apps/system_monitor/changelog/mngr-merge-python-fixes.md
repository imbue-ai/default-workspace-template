The project requires Python 3.12 or newer (was 3.11), matching the workspace floor: every workspace already runs 3.12, and the lock already required it.

The disk view's "System software" row now says it also holds the installed packages: every venv lives beside the uv cache, outside the home tree, so the row counts them.

The README runs the backend from `.venv/bin`; the editor format command and the avatar registration usage line use `uv run --no-sync`.

The update-staleness test reads the update apply contract at its new path inside the `update_self_skill` package.

The README's update-apply section points at that package, rather than at update-self's `scripts/`, as the apply's reference.

The README's update-apply section points at update-self's `update_self_skill` package in `.agents/skills/update-self/python/` as the reference code, where the scripts-as-packages change moved it.

The README starts the backend from `.venv/bin/system-interface` rather than under `uv run`, following the rule that a long-running process never runs under `uv run`.

The README now says the app runs from the workspace venv (its one install), not from its own uv tool.

The tool-call reader treats `uv run --no-sync` / `--frozen` / `--quiet` in front of `tk` the same as a bare `uv run`, so a tk step command run that way is still recognised (the option set is shared with the secrets guard through `tk_command_parsing`); the README runs the backend from `.venv/bin` and the tests with `uv run --no-sync`, and the editor format command uses `uv run --no-sync`.

The venv sync a failed chat create runs before its retry is `uv sync --all-packages --frozen`, so it never rewrites a `uv.lock` another agent has left out of date with `pyproject.toml`.

The tests that pin `message_chat.py` and `run_in_background.py` constants import them from the `workspace_bare_scripts` package, now a chat dev dependency, instead of loading the files by path, and the one that pins update-self's resume note imports `update_self_skill.update_resume_note` (`update-self-skill` is a chat dev dependency too).

The README and the account index lock's docstring no longer mention `migrate_claude_auth.py`, which this release deletes. The README and the background-task docs name `run_in_background.py` by its module path, `system/scripts/workspace_bare_scripts/run_in_background.py`, and the secret-request and background-task end-to-end tests run `request_secret.py` and `run_in_background.py` from their modules.

The README's development section starts the backend from `.venv/bin/chat-app` rather than under `uv run`, following the rule that a long-running process never runs under `uv run`.

The README now says the app runs from the workspace venv (its one install), not from its own uv tool.

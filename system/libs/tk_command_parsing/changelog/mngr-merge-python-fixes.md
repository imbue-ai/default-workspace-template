New `tk_command_parsing.uv_run.unwrap_uv_run` returns the program a `uv run` command starts, past the options that leave it unchanged (`--no-sync`, `--frozen`, `--quiet`); the secrets guard and the chat's tk-lifecycle detection both use it, so they agree on which `uv run` forms they see through.

The README and the parser's docstring name the guard checks that import this library at their new home, `system/scripts/workspace_bare_scripts/`; the hooks now reach them through `system/scripts/agent_hook_check.py`.

The project requires Python 3.12 or newer (was 3.11), matching the workspace floor: every workspace already runs 3.12, and the lock already required it.

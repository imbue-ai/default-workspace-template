The project is type-checked: its ratchets run `test_no_type_errors` (`ty`, pinned to 0.0.24 as the shell and the chat pin it), and its `pyproject.toml` makes the directory its own `ty` project (an empty `[tool.ty]` table).

`oom_drill.py` no longer crashes building its help text when Python runs with docstrings stripped (`-OO`); the description is empty instead.

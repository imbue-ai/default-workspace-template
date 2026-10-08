The project is type-checked: its ratchets run `test_no_type_errors` (`ty`, pinned to 0.0.24 as the shell and the chat pin it), and its `pyproject.toml` makes the directory its own `ty` project (an empty `[tool.ty]` table).

The registry reader's log line for a skipped row keeps its row-name lookup; `ty` 0.0.24 cannot type that lookup on a row read from TOML, so that one rule is ignored on that line.

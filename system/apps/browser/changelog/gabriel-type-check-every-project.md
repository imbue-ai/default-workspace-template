The project is type-checked: its ratchets run `test_no_type_errors` (`ty`, pinned to 0.0.24 as the shell and the chat pin it), and its `pyproject.toml` makes the directory its own `ty` project (an empty `[tool.ty]` table).

The Flask views that answer an error as a `(body, status)` tuple are annotated `ResponseReturnValue`; the pcmflux and pixelflux captures narrow the lazily imported module with `isinstance(module, ModuleType)` and raise the pipe's own error when it failed to import; an integration test's `log_message` override matches `BaseHTTPRequestHandler`'s signature.

The type-check ratchet runs `ty` 0.0.85 (was 0.0.24). 0.0.85 checks the objects the chat builds with `cls.__new__(cls)`, which 0.0.24 typed as unknown, and its findings are fixed:

- The OOM prioritizer reads a chat's process start through `_read_active_process_started_at`, which answers `None` for a chat with no active agent instead of passing `None` on as an agent id.

- The antigravity watcher's `build` assigns its attributes without re-annotating them; the class body already declares each one.

- The codex endpoint tests' fakes are subclasses (`_FakeCodexLedger` of `CodexMessageLedger`, and a `CodexHarnessSession` subclass that overrides `ensure_live` and `_live_ledger`) instead of lambdas assigned over a session's methods.

- `@contextmanager` functions are annotated as returning `Generator[X, None, None]` rather than `Iterator[X]`, which 0.0.85 reports as a deprecated overload.

- An inert mypy-style `type: ignore` in the config tests is gone.

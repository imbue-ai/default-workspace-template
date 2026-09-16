# Parallel harden passes

- `AGENTS.md` adds two rules. When a creation already does what is being asked,
  run it or extend it and run it rather than producing the same result by hand
  beside it. And a change to code a harden pass already covered extends that
  code's tests in the same commit.
- A workspace where the user has built their own app no longer fails
  `update-self`'s tests, and a bare `uv run pytest` no longer trips over the
  clone an interrupted live dispatch test leaves behind.

# Parallel harden passes

- The crystallize worker may split a harden pass across sibling sub-workers and
  merge them itself.
- `lead-proxy.md` says what to do after arming a worker poll: end the turn. A
  worker's report is never polled -- not with `sleep`, not with `find` over its
  reports directory, not with `mngr list` or `tmux capture-pane`.
- The live two-level dispatch test is skipped by default. It boots two real
  claude agents through a full gate round trip, and CI never ran it (no claude
  binary, no credential), so its only effect was minutes of wall clock and real
  API turns on every in-workspace `uv run pytest`. It also now clears away
  clones an interrupted run left behind.

# Parallel harden passes

- The crystallize worker may split a harden pass across sibling sub-workers and
  merge them itself.
- Splitting is now the worker's own size-based call rather than a standing order
  in every crystallize task file. The call is made on the parts a user would name
  separately -- the code that reads the data, the server, a front end with
  several views -- rather than on how much code each already holds, because a
  small ingestion layer can still be a worker's whole job once it is tested. A
  script or a toy app is hardened directly, since each sibling's venv converge
  and plugin install cost more than the parallelism saves at that size.
- `lead-proxy.md` says what to do after arming a worker poll: end the turn. A
  worker's report is never polled -- not with `sleep`, not with `find` over its
  reports directory, not with `mngr list` or `tmux capture-pane`.
- The live two-level dispatch test is skipped by default. It boots two real
  claude agents through a full gate round trip, and CI never ran it (no claude
  binary, no credential), so its only effect was minutes of wall clock and real
  API turns on every in-workspace `uv run pytest`. It also now clears away
  clones an interrupted run left behind.

# Parallel harden passes

- The crystallize worker may split a harden pass across sibling sub-workers and
  merge them itself. Splitting is the worker's own call rather than a standing
  order in every crystallize task file, made on the parts a user would name
  separately -- the code that reads the data, the server, a front end with
  several views -- rather than on how much code each already holds. A script or
  a toy app is hardened directly, since each sibling's venv converge and plugin
  install cost more than the parallelism saves at that size.

- A request that names the surface rather than the data ("visualize my roadmap
  from the export I uploaded") starts at `fetch-process-show`, and `build-app`
  sends an app that reads outside records there before it plans anything. An app
  whose ingestion was hand-written inside `build-app` leaves nothing anyone can
  re-run when the next batch of data lands.

- A lead ends its turn once a worker poll is armed rather than sleeping on it,
  never polls a worker's report by other means, and refreshes the user's tab on
  a milestone itself instead of asking them to.

- None of the worker machinery reaches the user. Leases, polls, milestones,
  merges, gates and worker names are how work gets done, not what got done, so
  they belong in no message and no progress step.

- Several skills pointed at things that were not there -- a design skill named
  without the prefix that makes it resolve, a helper script at a path that has
  never existed. Both cost an agent its first minutes on work it should not have
  had to do.

- The live two-level dispatch test is skipped by default. It boots two real
  claude agents through a full gate round trip, and CI never ran it, so its only
  effect was minutes of wall clock and real API turns on every in-workspace
  `uv run pytest`.

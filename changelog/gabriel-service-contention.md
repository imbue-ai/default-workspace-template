- Added contention rules for concurrent service modification: when two chats
  each edit the same service and each dispatch a background harden pass, the
  foreground edits could interleave destructively in the shared working tree,
  and the harden passes could collide (worker name, branch, and runtime dir
  are all derived from the service name) or merge a branch verified against a
  base that had since moved.

- Foreground edits are now serialized by an advisory per-service **editing
  lease**: a regular `tk` ticket (`editing service <name>`, cross-agent
  visible) taken by `update-service` before touching the service's code and
  released at the end of each editing turn. An agent finding another agent's
  lease surfaces it to the user instead of silently proceeding; abandoned
  leases are broken deliberately by the user's call, never silently.

- Background harden passes are now **single-flight per artifact with
  coalescing**, specified in the new shared reference
  `.agents/shared/references/harden-contention.md` (read by `update-artifact`
  and `heal-artifact` at dispatch and at merge):
  - Before dispatch, a lead finding a live pass for the same target leaves a
    note on its tracking ticket instead of launching a sibling worker.
  - Before merge, the lead waits out any foreground editing lease, then runs
    a freshness check (`git merge-base` + `git diff` over the artifact's
    footprint); a pass whose base moved is stale.
  - A conflicted merge of a hardened branch is never hand-resolved (that
    would reintroduce exactly the unverified state hardening exists to
    prevent). Stale or conflicted passes are discarded and superseded by one
    new pass covering everything since the last hardened merge, so the
    combination that gets merged is the combination that was verified.
  - Priority rule throughout: the foreground always wins -- a live edit never
    waits on an in-flight pass; it just makes that pass stale.

- `lead-proxy.md`'s merge step now defers to a calling skill's staleness rule
  on conflict instead of unconditionally saying "resolve manually".

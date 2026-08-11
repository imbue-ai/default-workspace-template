Reworked `update-system-interface` from a delegate-then-preview-before-merge
flow into a lead-driven **live editing loop**. The lead now edits an isolated
git worktree, builds, and refreshes a labeled preview tab in place (seconds per
round), iterating with the user; only once the user approves the shape does a
background worker run the full test + review gate *on that same branch*, after
which the change is merged and safe-revealed. The editing lease is held across
the whole pass (entry through reveal), since there is one served UI and one
preview tab.

- `serve_isolated_instance.py` gained a `refresh` command: it re-boots just the
  inner server on its existing port so a rebuild or edit is picked up in place,
  leaving the port, the wrapper frame, the service registrations, and the user's
  tab untouched. It waits for the old process to release the port before
  rebinding, and records the new pid before the health wait so a later `down`
  can still reap it if it never comes up.

- `reveal_system_interface.py` gained `preview-refresh`: bounce the preview's
  inner app for a backend round without disturbing the wrapper or the tab.

- `create_worker.py` (`launch-task`) gained a `--branch` passthrough to
  `mngr create`, so the harden worker can check out and extend the branch the
  lead already built up instead of branching anew from HEAD. The branch that
  `launch-sync` publishes for callers to merge from is now **read back from the
  created agent** (`read_worker_branch`, via `mngr ls --format json`) rather than
  assumed to be `mngr/<name>` -- with a spec that renames or reuses a branch, the
  old assumption named a branch the worker never committed to. It reads mngr's
  `branch` field, not `initial_branch`: the latter is only set for a branch mngr
  *created*, and is None for the checked-out-existing-branch case these callers
  use. The read happens right after launch, before the await, so a worker whose
  branch mngr cannot report fails immediately instead of after a whole run; and
  it raises rather than guessing, since a wrong answer sends the caller to merge
  a ref that does not exist. The `launch-task` skill documents the flag.

- `op-update.md`'s system-interface exception was retargeted at the new handoff:
  the worker's branch already carries the user-approved change, and the task says
  which of two shapes applies -- "implement the approved shape for real, then
  harden", or "harden only: verify, do not re-implement". Previously it told
  every system-interface worker to implement the brief, which on a harden-only
  handoff meant redoing work the user had already signed off on.

- The preview and the reveal pre-flight now **follow** the live agent-lifecycle
  event stream instead of competing for it. Both boot a second system interface
  beside the live one, which holds the single-writer `mngr observe` lock, so both
  used to come up with a permanently frozen agent view -- and both used to pass
  their health probe anyway, because `/api/agents` runs its own discovery and
  never looks at the lifecycle stream. They now launch with
  `SYSTEM_INTERFACE_AGENT_EVENTS_MODE=FOLLOW` and gate on the new strict
  `/api/health`, which stays red unless that stream is really feeding the
  instance. A preview whose lifecycle stream cannot be established therefore does
  not come up at all: a silently frozen preview is worse than no preview, because
  the user reads it as the real UI. The *live* service's post-restart and recovery
  probes deliberately keep the looser `/api/agents` -- a rollback there is heavy,
  and lifecycle-stream trouble on the live UI is not something reverting a UI
  change would fix.

- `serve_isolated_instance.py` now quotes the tail of the boot log on stderr when
  an instance (or the preview wrapper, or a `refresh`) fails to become healthy,
  instead of only naming the log file. The caller reading that stderr is an agent,
  so the reason for the failure is now in front of it.

- `interactive-delivery.md` phase 5 was recast around **fast feedback**, with
  two demonstrative-prototype types chosen by wiring-cost vs. restart-cost: a
  Type 1 "janky real edit" (rough, but in the real code and shown through the
  real surface) and a Type 2 "detached prototype" (a throwaway mock). `update-app`
  picked up that taxonomy plus guidance that a preview is the exception, not the
  default (reserve it for changes costly to redo); `update-creation` and
  `update-self` were updated to match the live-loop handoff onto the lead's
  branch.

- `update-system-interface` is now genuinely deltas-only, and says so: it opens by
  telling you to read `update-app` first, then names only what differs. The
  material it used to restate -- the lease pre-flight, the `frontend-design` /
  `use-ai-integration` rules, the `--layout` explanation, the fast-pytest-marker
  invocation -- is gone, leaving the lease section as three bullets (the fixed
  service name, held-for-the-whole-pass, and teardown of an orphaned pass on
  break). Nothing told the agent to load `update-app`, so the duplication was what
  made the skill self-sufficient; the pointer had to become an instruction before
  it could be cut.

- **The final pre-merge preview is gated on whether the user can actually judge
  the change**, not on it being the system interface. Both conditions must hold:
  the worker produced real work the user has not seen, *and* they can observe and
  judge what changed. The question a preview appears to answer first -- does it
  boot -- is already answered mechanically by safe-reveal's health check and
  auto-rollback, so the user's eyes only add "does this look right". A fix for a
  race, an error path, or a scenario they cannot trigger from a tab gives them
  nothing to look at, and asking them to approve an apparently unchanged UI
  teaches them that approving a preview means nothing; the evidence there is the
  regression test the harden gate already produced. This is the Step 2 test-only /
  no-surface carve-out applied at merge time, and it restores the two-part gate the
  plan specified (the skill had collapsed it to "essentially always").

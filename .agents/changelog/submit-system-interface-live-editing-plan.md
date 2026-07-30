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

- `reveal_system_interface.py` gained `preview-refresh` (bounce the preview's
  inner app for a backend round without disturbing the wrapper or tab) and now
  seeds each preview from a throwaway copy of *only* the live layout files, so
  the preview opens with the user's real tabs while its own layout autosaves
  land in the copy rather than clobbering the live layout; `unpreview` removes
  that copy. The live layout is located by finding the workspace's *primary*
  (`is_primary=true`) services agent -- the one the system interface itself runs
  under, and the only agent that owns a `workspace_layout/` -- rather than by
  deriving a path from the ambient `MNGR_AGENT_ID`, which names whichever agent
  ran the script. Since the frontend hides `is_primary` agents from the agent
  list, that is always a different agent with no layout of its own, so the
  derived path never existed and every preview silently seeded nothing and opened
  with default tabs. When there is genuinely nothing to seed, `preview` now says
  which of the two reasons it was on stderr: an empty seed and a working preview
  look identical on screen, which is precisely what hid this.

- `create_worker.py` (`launch-task`) gained a `--branch` passthrough to
  `mngr create`, so the harden worker can check out and extend the branch the
  lead already built up instead of branching anew from HEAD. The branch that
  `launch-sync` publishes for callers to merge from is now derived from that spec
  (`resolve_worker_branch`, mirroring mngr's own `[BASE][:NEW]` parsing) rather
  than assumed to be `mngr/<name>` -- with a spec that renames or reuses a
  branch, the old assumption named a branch the worker never committed to. The
  `launch-task` skill documents the flag, and a malformed spec raises
  `BranchSpecError` before any worker is created.

- `op-update.md`'s system-interface exception was retargeted at the new handoff:
  the worker's branch already carries the user-approved change, and the task says
  which of two shapes applies -- "implement the approved shape for real, then
  harden", or "harden only: verify, do not re-implement". Previously it told
  every system-interface worker to implement the brief, which on a harden-only
  handoff meant redoing work the user had already signed off on.

- The layout copy is verbatim, including a layout that opens the preview tab
  itself -- which is nearly all of them, since that tab stays open for the whole
  editing pass. Rendering it would make the preview show *itself* (its inner app
  resolves `service:si-preview` against the same live registry, so the panel
  proxies back to the wrapper framing it, unboundedly). The previewed instance
  refuses that instead, via the new
  `SYSTEM_INTERFACE_SELF_REFERENTIAL_SERVICES`: that one tab shows a line saying
  it is the preview you are already looking at, and the rest of the layout is
  exactly as the user has it. Consequently `preview` needs no tab bookkeeping
  before a re-run, and the skill's "close the tab first" step is gone.
  A `preview` that fails to boot removes the layout copy it had already seeded,
  instead of leaving it for an `unpreview` that is never coming.

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

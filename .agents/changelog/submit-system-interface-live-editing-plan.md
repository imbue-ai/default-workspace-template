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
  that copy.

- `create_worker.py` (`launch-task`) gained a `--branch` passthrough to
  `mngr create`, so the harden worker can check out and extend the branch the
  lead already built up instead of branching anew from HEAD. The branch that
  `launch-sync` publishes for callers to merge from is now derived from that spec
  (`resolve_worker_branch`, mirroring mngr's own `[BASE][:NEW]` parsing) rather
  than assumed to be `mngr/<name>` -- with a spec that renames or reuses a
  branch, the old assumption named a branch the worker never committed to.

- The preview no longer seeds a saved layout that itself opens a preview panel.
  Such a layout made the preview render *itself* (its inner app resolves
  `service:si-preview` against the same live registry, so the panel proxies back
  to the wrapper framing it). The layout stays registered and simply opens empty.
  A `preview` that fails to boot now also removes the layout copy it had already
  seeded, instead of leaving it for an `unpreview` that is never coming.

- `interactive-delivery.md` phase 5 was recast around **fast feedback**, with
  two demonstrative-artifact types chosen by wiring-cost vs. restart-cost: a
  Type 1 "janky real edit" (rough, but in the real code and shown through the
  real surface) and a Type 2 "detached prototype" (a throwaway mock). `update-service`
  picked up that taxonomy plus guidance that a preview is the exception, not the
  default (reserve it for changes costly to redo); `update-artifact` and
  `update-self` were updated to match the live-loop handoff onto the lead's
  branch.

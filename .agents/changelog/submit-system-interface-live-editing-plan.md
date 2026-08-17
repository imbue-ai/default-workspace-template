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

- The live loop calls that shared `refresh` directly, and `reveal_system_interface.py`
  no longer wraps it -- nor `down`, which it used to expose as `unpreview`. Both
  wrappers took a slug and did nothing with it but build the instance name
  `si-preview-<slug>`, so each was a second place that had to agree with the
  convention `preview` already owns. The flow now addresses the instance by the
  name `preview` prints on success, and the script keeps only the two
  sub-commands that carry real logic: `preview` (work_dir validation, the
  system-interface env spec, the one-preview-at-a-time guard) and `reveal`.

- **`layout.py open` is now documented as the act of showing the user
  something, not as setup.** It mutates the workspace they are looking at, live,
  the moment it returns -- but nothing said so, so an agent could open a preview
  tab, keep driving it with Playwright as though the window were still private,
  and then finish by telling the user to open the tab it had opened for them ten
  minutes earlier. `CLAUDE.md` and `manage-layout` (including its description, so
  it lands in context even for an agent that never loads the skill) now state the
  consequence, and `update-system-interface`'s first round separates the boot
  from the open: check the boot's exit code -- the strict health gate is the
  verification -- and only then surface it.

- **`update-app`'s Verify step now says *when* to verify: before the user can
  see it, not after.** The step is inherited by every app flow including the
  system interface, and it prescribes a Playwright assertion -- which is right
  in `build-app` (verify is Step 3, surfacing the tab Step 4) and wrong once a
  surface is already in front of the user, where the user is the verifier and a
  scripted interaction lands in their view and their state. `interactive-delivery.md`
  phase 5 carries the same rule for prototypes generally: the lead's own check
  ends at "it came up", and the thorough pass belongs to the phase-7 worker,
  which runs against its own instance.

- `create_worker.py` (`launch-task`) gained a `--branch` passthrough to
  `mngr create`, so the harden worker can check out and extend the branch the
  lead already built up instead of branching anew from HEAD. The branch that
  `launch-sync` publishes for callers to merge from is now **read back from the
  created agent** (`read_worker_branch`, via `mngr ls --format json`) rather than
  assumed to be `mngr/<name>` -- with a spec that renames or reuses a branch, the
  old assumption named a branch the worker never committed to. It reads mngr's
  `initial_branch` field, which now reports the branch the work_dir was placed on
  whether mngr created it or checked out one that already existed -- including the
  checked-out-existing-branch case these callers use. The read happens right after launch, before the await, so a worker whose
  branch mngr cannot report fails immediately instead of after a whole run; and
  it raises rather than guessing, since a wrong answer sends the caller to merge
  a ref that does not exist -- destroying the just-created worker first, so a
  branch it cannot name does not leave a live agent behind (an orphan also wedges
  the next call, since `launch` refuses a stale report and `mngr create` refuses
  the duplicate name). That raise now reaches `launch-sync`'s caller as exit 2
  like every other failure rather than as a traceback, a destroy that itself fails
  reports both facts instead of replacing the original cause, and the worker name
  is validated against mngr's own name rules before being interpolated into the
  `mngr ls --include` filter -- a quote there reshapes the CEL expression rather
  than failing, and the empty listing that comes back is indistinguishable from
  "no such agent", which is the branch that destroys the worker. The lookup also
  stopped treating `mngr ls`'s exit code as the verdict: listing continues past
  provider failures and only then exits non-zero, so one unreachable or
  unauthenticated provider anywhere in the config used to read as "could not
  look" -- destroying a healthy worker that was sitting in the payload the
  command had just printed. The branch is now read from the payload itself, and
  an empty listing quotes the payload's errors channel, so "the agent is gone"
  and "the provider holding it could not be reached" no longer look alike. The
  `launch-task` skill documents the flag.

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

- **A failed boot now states its cause and points at a log that still exists.**
  `serve_isolated_instance.py` quotes the tail of the boot log on stderr when an
  instance (or the preview wrapper, or a `refresh`) fails to become healthy, and
  three things make that tail worth reading. The health probe keeps the response
  *body* instead of only its status code, so a refusal's own sentence ("No 'mngr
  observe' process holds ...") is the first thing in the message rather than a
  bare `503` buried in access-log lines. Each spawn writes a boundary marker into
  the (append-only, refresh-reused) log and the excerpt is scoped to the last one,
  so a reboot that hangs at import shows *nothing* -- the truthful signal -- rather
  than the previous boot's traceback, which sent an agent hunting a cause no longer
  in the source. And a failed `up` copies the log to
  `data/.state/isolated-instances/<name>-failed.log` before the teardown deletes
  the state directory, so the path the message names is still there when the agent
  goes to read the rest of it.

- **`down` now verifies the process actually died, and says so when it did not.**
  It sent SIGTERM and never looked, so a service that traps SIGTERM was reported
  torn down while it kept serving on a port that stayed bound -- and the state file
  naming its pid was deleted, leaving nothing able to find it again. It now
  escalates (SIGTERM, wait, SIGKILL, wait) and only removes the state directory
  once every recorded process group is confirmed gone; a survivor keeps the state,
  names its pid, and exits non-zero. `up`'s partial-instance teardown escalates the
  same way, and `up` refuses to boot over a stale instance it could not clear.

- **An isolated instance is tagged into the `user` service OOM band (200).** It is
  launched directly rather than as a supervisord program, so nothing else banded
  it: it inherited the launching shell's band, and every Claude bash command
  self-tags `AGENT_SUBPROCESS` (900) -- making the surface the user is actually
  looking at the first non-browser thing shed under memory pressure, with nothing
  re-polling health afterwards to tell them their tab had died. Both the inner
  server and the preview wrapper are prefixed with the existing
  `oom_tag_service.py user` wrapper, the same way `build-app` scaffolds a service.
  The tradeoff is deliberate: at 200 the instance outlives every agent, including
  the lead driving it.

- **`reveal` restarts only the `system_interface` service, and its health verdict
  is settled state.** It ran `mngr start --restart system-services`, and the
  services agent *is* supervisord's parent -- so a dependency-only change bounced
  every program in the workspace, and a 30s health budget then raced a whole-stack
  restart storm. The dependency refresh touches this app's own venv and
  `node_modules`, which nothing else consumes, so it is now
  `supervisorctl restart system_interface`. The verdict that arms the automatic
  rollback also stopped being a point-in-time probe: it requires several
  consecutive healthy answers on an unchanging supervisord pid, over a 60s budget
  matching the shared serve script's boot gate. Both directions were expensive --
  the same cosmetic manifest change revealed twice reported "confirmed healthy"
  while the pid was still turning over, then auto-rolled-back a change that was
  never broken. The post-rollback "the live UI is confirmed healthy" claim uses
  the same settled check.

- **`update-system-interface` now says what to do when the hand-off cannot be
  delivered.** `layout.py open` is the act of showing the user the preview, but
  nothing covered every layout refusing it because no client is connected -- so a
  lead improvised (verify privately, surface a screenshot) and then asked for
  approval as though the user had seen the live surface. The skill now sanctions
  the screenshot fallback *as* a fallback: say plainly that the live tab could not
  be put on their screen, that approval on a still image is weaker, and re-attempt
  the `open` rather than treating the screenshot round as the delivered preview.

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

- **Three doc defects that cost two independent leads real time.**
  `update-creation` and `heal-creation` spelled `create_worker.py await` without
  its required `--name`, which argparse rejects -- and the background runner
  reported the failed command as "completed (exit code 0)", so both leads read it
  as the worker finishing. `type-system-interface.md` now says never to use
  Playwright's `wait_until="networkidle"` against a system-interface instance: it
  holds live connections, so the network never goes idle and the call burns its
  whole timeout. And the "no `## Change origin` marker" exception is now stated in
  `update-creation`, where the task-file format is *defined* and where a lead
  actually looks, instead of only in `update-system-interface`'s Step 3 -- a lead
  that read the general rule wrote the marker anyway and re-armed the gate the
  exception exists to skip.

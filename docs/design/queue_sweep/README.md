# Queue sweep: truthful message queues across claude, codex, and pi

## The contracts

Every harness parks a message sent mid-turn in its OWN in-process queue -- Minds never
delivers mid-turn text out-of-band. That implicit enqueue mutation ("typing while the
agent works queues; it does not interrupt") is the ground truth the three explicit
contracts govern:

- **Contract A -- the mirror invariant.** The queued messages Minds shows for an agent
  exactly match the LIVE harness process's parked queue, scoped to the current process
  session. Process dead -> queue shown empty, even though durable ledgers keep history.
- **Contract B -- stop button = interrupt-and-retract.** Stop interrupts the running
  turn, returns the queued messages to the user's composer, and leaves the harness's
  own queue empty.
- **Contract C -- shoulder tap = commit.** The tap flushes every currently-queued
  message into the live session now.

## The two restart cases

Contract A hinges on distinguishing two restarts that look alike on disk:

- **Backend restart, harness alive mid-turn:** the mirror must be REBUILT to the true
  parked set by replaying durable ledgers (claude's in-transcript queue-operation
  ledger, codex's queued_input.jsonl, pi's pi_inbox).
- **Agent process stop/restart:** the queue died with the process; the mirror must show
  EMPTY even though the same ledgers still hold the dead generation's entries.

Each mirror plan scopes its replay to the live process generation (claude: latest main
session id; codex: process-start marker mtime vs ledger timestamps; pi: inbox
truncation at extension load), and all three rely on one shared pre-broadcast
idle-sweep trigger for the dead-process case.

## Plan index

- **plan-claude-queue.md** -- scopes claude's queue replay to the latest main session,
  fixes the activity-seeding order, and adds the shared pre-broadcast idle-sweep
  trigger (owned here; all three mirrors rely on it). dwt only (`claude-codex-pi-dwt`);
  no rebuild.
- **plan-codex-queue.md** -- generation-scopes the codex ledger replay by process-start
  marker mtime; dead lifecycle forces IDLE at the manager. dwt only
  (`claude-codex-pi-dwt`); no rebuild.
- **plan-pi-queue.md** -- pi extension truncates the durable inbox at load (mngr,
  `claude-codex-pi-mngr`, plus a one-off migration for existing agents); watcher scopes
  leave-pops to the current generation (dwt, `claude-codex-pi-dwt`); no rebuild.
- **plan-pi-interrupt.md** -- stop button goes native for pi: a retract sentinel on
  pi_inbox replaces the SIGKILL-restart; introduces the per-harness
  interrupt-to-composer registry (base = shared restart-drain, kept by claude). mngr
  extension (`claude-codex-pi-mngr`) + dwt server/watcher/frontend
  (`claude-codex-pi-dwt`); no rebuild.
- **plan-codex-interrupt.md** -- stop button goes native for codex: a `retract_turn_id`
  control line on the fork's existing shoulder-tap channel; codex registers its
  registry override. codex fork (patch regen, both-arch rebuild, sha256 repin) + dwt
  server (`claude-codex-pi-dwt`); rebuild REQUIRED.
- **plan-claude-tap.md** -- shoulder tap goes native for claude: a Chat-only `meta+q`
  -> `chat:cancel` chord delivered via tmux, a 3s watcher, and a short recovery message
  for the cancelled-follow-on race; keybinding provisioning in mngr
  (`claude-codex-pi-mngr`), tap executor in dwt (`claude-codex-pi-dwt`); flips
  `native_atomic_shoulder_tap_possible` True for claude; no rebuild. Gated (below).
- **plan-claude-interrupt.md** -- stop button goes half-native for claude: an EMPTY
  queue mid-turn gets the tap plan's `meta+q` chord (pure interrupt, confirm-then-clear
  of the stranded `active` marker, both interrupt-sentinel shapes suppressed in the
  parser); a NONEMPTY queue keeps the restart-drain base. Amends plan-pi-interrupt's
  pinned claude-empty-queue test and adds the tap's recovery-suppression guard. dwt only
  (`claude-codex-pi-dwt`); no rebuild.
- **plan-composer-hint.md** -- mid-turn composer placeholder becomes "Type to queue
  more messages..." -- the contracts' user-facing legend. dwt frontend only
  (`claude-codex-pi-dwt`); no rebuild.

## Landing order

1. **plan-claude-queue first (hard edge one):** its pre-broadcast sweep trigger and
   seeding reorder are assumed by both sibling mirrors; landing a mirror without them
   leaves dead-process phantoms unswept.
2. plan-codex-queue and plan-pi-queue, in either order.
3. plan-pi-interrupt: introduces the interrupt-to-composer registry the codex override
   plugs into.
4. **plan-codex-interrupt strictly after plan-codex-queue (hard edge two):** the
   handback trusts the mirror, so prior-generation orphans must already be scoped out.
   It also follows plan-pi-interrupt, whose registry it registers into.
5. plan-claude-tap once its decision gate passes; plan-claude-interrupt after
   plan-claude-queue, plan-pi-interrupt, AND plan-claude-tap (it reuses the tap's
   provisioning, executor module, and gate trace); plan-composer-hint any time (its
   dead-codex correctness completes when plan-codex-queue move 2 is in).

## The one open decision point

plan-claude-tap gates on a single manual verification: what `chat:cancel` does to a
non-empty queued-message box on claude 2.1.207 (the gate trace must include a mid-tool
interrupt, pinning both interrupt-sentinel shapes). If cancel flushes the queue through
(the expected outcome), the tap and interrupt plans land as written. If cancel instead
returns the queue to claude's own composer, the chord serves Contract B in full: the
tap keeps restart-flush (flag stays False for claude) and plan-claude-interrupt's
nonempty branch also goes chord-native, retiring the restart-drain's last claude
caller -- the pivot is recorded in both plans' Open risks; no dual design.

`native_atomic_shoulder_tap_possible` stays in the catalog in every outcome: it is the
dispatch point for the tap's base (restart) path, which future harnesses may need.

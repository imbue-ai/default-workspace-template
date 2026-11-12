# Answering the worker's `question` gate (SKILL.md Step 4)

Read this when the poll delivers a report whose `name:` is `question`. The
shared mechanics of answering a worker are in
`data/.tasks/update-self/skill-at-target/.agents/shared/references/lead-proxy.md`
(worker `update-self`, branch `mngr/update-self`, reports dir
`data/.tasks/update-self/reports/`).

A `question` is one of four things, and its first line says which. You answer
the first two yourself. You answer the third too, and reach the user only
where it says so. The fourth always ends the pass with a decision for them.

## Contents

- Answering and re-arming
- 1. A genuine, unresolvable merge conflict
- 2. The worker's scope escape hatch
- 3. A gate command shed for memory
- 4. A customization the update cannot keep (a hold)
- A hold ends the pass with a decision

## Answering and re-arming

For the first three kinds: reply with `create_worker.py reply --task-file
data/.tasks/update-self/task.md -m "..."`, re-arm the poll
(`launching-the-worker.md`, section 5) and end your turn. Where the launcher
has no `reply` subcommand, or the report file is still in place after the
poll delivered it, `older-workspaces.md` has what to do first.

## 1. A genuine, unresolvable merge conflict

Decide it yourself. The default is to **keep this workspace's current
behavior**: preserve the local customization, fold in what upstream adds
around it, and take the release's side only where no local intent is at
stake. Record every decision (the file, what was kept, what the release's
version would have changed): the results message presents each with the
alternative still on offer. A conflict where *every* resolution breaks
something the user built is not a merge question; it is the hold below.

## 2. The worker's scope escape hatch

A process question from its Step 4a, 4b or 4c: whether its impact analysis
runs, whether a validation item runs, or whether and at what scope the review
gates run. Answer it by the rule it names, as written. Where the rule is
silent, the fallback is more coverage, never less. A `select-tests` output is
not silence: what it printed for a commit is that commit's whole gate.
Escalate to the user only if it contains a real question of their intent.

## 3. A gate command shed for memory

The worker is asking for room to rerun. Free it yourself per
`freeing-memory-for-an-update.md`, which says what this pass may stop and how
much further a scheduled pass may go. Then reply with what you stopped and
the free memory the candidates command reports *after* it. Memory pressure is
never a reason to narrow the gate, so never answer by telling the worker to
skip or trim one.

It reaches the user only when the licence is spent and the room is still not
there. Then ask them, naming what is left to stop and what stopping it costs
them. Record the wait with the staged copy's `run-status hold --detail "<one
plain line>"` while you ask, and clear it with `run-status resume` when they
answer. The worker keeps its branch and waits; this is not a hold.

## 4. A customization the update cannot keep (a hold)

The worker's Step 4b verdict: something the user built that the update
**cannot keep**, after the worker genuinely tried to re-fit it. This is the
one gate that reaches the user, and it ends the pass. A cosmetic shift (the
widget moved but still works) is not a hold: it applies unattended and is
named in the results message with an offer to restore.

## A hold ends the pass with a decision

Nothing waits for the user's answer holding a lease or a live worker, however
long they take. In this order, each command on its own:

1. Record the verdict. The detail line is shown in the app, so write it for
   the user:

   ```bash
   python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py \
       run-status verdict NEEDS_DECISION \
       --detail "<one plain line: what they built that the update cannot keep>"
   ```

2. Stop the worker, keeping it. Its branch `mngr/update-self`, its worktree
   and its conversation are what re-land the update once they decide, so
   never destroy it or delete the branch here:

   ```bash
   mngr stop update-self
   ```

   The poll re-armed when they answer must wait for the worker's reply, not
   return this hold again: if its report file is still in place, consume it
   as `older-workspaces.md` ("Consuming a report by hand") says.

3. Run teardown's opening commands (`teardown.md`, "Every ending"). They drop
   the history bridge, bring back any chat this pass paused, and start any
   app it stopped for memory: the user may take days over this decision, and
   the workspace has to run as usual while they do. Then release the
   `updating workspace` lease: `tk close "$UPDATE_LEASE_ID" "Waiting on the
   user's decision."`. Leave the tracking ticket open; the re-land closes it.

4. Compose the decision message from their point of view per
   `results-message.md` ("The decision message"), show this chat as teardown
   ends ("Show the user this chat"), and end your turn.

Their answer, whenever it comes, arrives in this chat: follow
`resuming-after-a-decision.md`.

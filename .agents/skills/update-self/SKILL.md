---
name: update-self
description: Safely pull updates from the upstream template repo (default target is the release the running Imbue Studio app was built against). Use when you want to incorporate upstream skills, script fixes, or config improvements. For pushing local improvements back upstream, use the `submit-upstream-changes` skill instead.
metadata:
  author: imbue
---

# Pulling updates from the upstream template, safely

This repo was created from a template repo and stays connected to it via a git
remote (`system/config/parent.toml` has the URL and branch). Upstream carries
the shared infrastructure: skills, scripts, `CLAUDE.md` scaffolding,
`Dockerfile`, `system/supervisord.conf` and its `supervisord.conf.d/` drop-ins,
the system interface, `mngr`.

Merging upstream can break the live workspace, so this flow never mutates the
live tree from an unverified state: an isolated **worker** does the merge and
validation on its own branch, and only a validated result is landed by one
atomic, rollback-on-failure **apply**.

You are the **lead**, and the pass is **fully unattended**: resolve the target,
dispatch the worker, answer its gates, audit its evidence, run the apply, then
report. Nobody is watching: the user comes back to a finished result and an
offer to roll back anything they don't like. One question still waits for
them inside a pass: an `--override` past the version ceiling (asked at
launch, while they are present). An update that cannot keep something they
built does not wait: the pass ends with a decision for them (Step 4), and
their answer, whenever it comes, re-lands it.

The default target is **the release the Imbue Studio app driving this workspace was
built against**, and only that one: the template ships the code that app talks
to, and no other pairing was verified. See `references/version-ceiling.md`. Once the target is resolved,
the pass **re-points itself at the target version's own copy of this skill**
(Step 2a) and runs the rest -- lead and worker -- from the fixed staging path
`data/.tasks/update-self/skill-at-target/.agents/skills/update-self`, so fixes
to the update flow that shipped in the release are applied on the way in.
Address that path by literal each time (each bash invocation is a fresh shell).

## 1. Preconditions

**Check the backup first.** The backup service's periodic restore point of the
whole workspace is the last-resort recovery path if the apply's own rollback
and `recover` both fail. Confirm there is a recent one; this reads the
service's log and waits for nothing:

```bash
uv run host-backup-now --check
```

Exit 0 means a recent `restic_backup_succeeded`; 3 (not configured), 1 (no
recent success: the service is down or its ticks fail) and 2 (not observable)
mean there is **no** confirmed restore point. **None of them blocks the
pass**: note which it was, carry it into the results message as a caveat, and
continue -- git still holds every version of the tree. Do not stop to ask for
a go-ahead.

**What this pass may do without asking.** The update is the user's request,
so it carries their licence to clear the way for it. On your own judgment of
what this run needs, with no confirmation and no preflight script, you may:

- stop other chats and background agents, idle or running, through `run-status
  stop-chats` (5b, and earlier with `--agent <name>` when memory runs short),
  which records each so the end of the pass starts it again with a note;
- stop the browser and the user apps no window is showing, and on a **scheduled**
  run any user app at all, windows or not
  (`python3 system/services/oom_priority/bin/memory_candidates.py` lists them;
  a browser stops through the browser service, an app through `supervisorctl
  stop <program>`). `references/freeing-memory-for-an-update.md` has the rule,
  how to tell a scheduled run from a pressed one, and what the pass owes the
  user afterwards;
- prune the worktrees of workers that are gone (`git worktree list`, then `git
  worktree remove` one whose agent `mngr list` no longer shows, never the
  `update-self` worker's), apply snapshots under
  `data/.state/update-apply/snapshots/` that no `marker.json`,
  `emergency.json` or `last-good.json` beside them names, and tool caches
  (`uv cache prune`, `npm cache clean --force`);
- commit a dirty tree as one work-in-progress commit (below).

Do what the run needs and no more, and name every one of these actions in the
results message with its undo (`references/results-message.md`). This licence
is this pass's alone: every other flow still asks first
(`.agents/shared/references/freeing-memory.md`).

**Take the "updating workspace" lease.** One update flow at a time (worker
name, branch and runtime dir are fixed, and two applies must never interleave).
Check for a foreign one first:

```bash
tk ready > /tmp/update-self-inflight.txt
grep "updating workspace" /tmp/update-self-inflight.txt
```

A match is decided by rule, never by asking. `tk show <id>` names its holder
(`Held by <name>`). A lease held under your own name is this chat's from an
earlier attempt: close it (`tk close <id> "Superseded: its holder is gone."`)
and take your own. Any other holder is **live** when
`mngr list --format "{name}	{state}"` shows that agent `RUNNING` or `WAITING`:
another update is running, so tell the user that one is already under way and
end this pass, recording nothing (the run record is that pass's). Otherwise its
holder is gone: take the pass over as its lead, or supersede it once its worker
is gone too, per `.agents/shared/references/harden-contention.md`, without
stopping to ask. With no match, or once your own is closed, take it (each `tk`
call as its own command, never chained):

```bash
UPDATE_LEASE_ID=$(tk create "updating workspace" -t chore \
    -d "Held by $MNGR_AGENT_NAME across the update-self pass; released at teardown.")
```

then `tk start "$UPDATE_LEASE_ID"`.

**Record the run for the Imbue Studio app** -- as soon as the lease is yours, so the
app can see a run is under way:

```bash
python3 .agents/skills/update-self/scripts/update_self.py run-status start
```

This writes `data/.state/update-apply/run.json`, which the app polls. Besides
the `hold`/`resume` pair around the one wait on the user (the override
confirmation in Steps 2 and 3a) and the chats 5b pauses and resumes, the pass
owes that file exactly one more write, a `run-status verdict` when it ends;
every terminal path below names its verdict, and a pass that ends without one
shows the user "update failed". It comes after the lease check because there
is one record per workspace, and a pass that stops because someone else is
updating must leave the running pass's record alone.

**Save uncommitted work.** The worker branches off your `HEAD` and the rollback
captures it, so uncommitted changes would be left out of both. If `git status
--porcelain` is non-empty, commit everything as one work-in-progress commit on
the branch the workspace is on, its message listing the paths, and note the
sha it prints for the results message:

```bash
git add -A
{ echo "Save work in progress before the workspace update"; echo
  echo "Uncommitted changes, saved as they stood so the update could run:"
  git diff --cached --name-only | sed 's/^/- /'; } > /tmp/update-self-wip-message.txt
git commit -F /tmp/update-self-wip-message.txt && git rev-parse HEAD
```

If a hook refuses the commit, take the file it names back out (`git reset --
<path>`), commit the rest, and name the file left uncommitted in the results
message.

## 2. Resolve the target

**A pinned update.** When the message that started this pass pins the update,
set `PINNED_CEILING` below to its `Ceiling:`, and `PINNED_OVERRIDE` to its
`Target:` when the message says the user chose that version. `resolve-target`
then never asks the app, and an app this pass cannot reach is not a fault.
Otherwise leave both empty.

Ensure the remote exists, heal a shallow clone, fetch with tags, and resolve:

```bash
PINNED_CEILING=""
PINNED_OVERRIDE=""
git remote get-url upstream 2>/dev/null || git remote add upstream "$(python3 -c "
import tomllib
with open('system/config/parent.toml', 'rb') as f:
    print(tomllib.load(f)['url'])
")"
# Workspaces created from a --depth 1 pool bake cannot answer "what changed
# since the fork point"; complete the history first (a no-op elsewhere). See
# references/heal-shallow-history.md.
if [ -f "$(git rev-parse --git-common-dir)/shallow" ]; then
    git fetch --unshallow upstream
fi
git fetch upstream --tags --force

python3 .agents/skills/update-self/scripts/update_self.py resolve-target --local-tags \
    ${PINNED_CEILING:+--app-version "$PINNED_CEILING"} \
    ${PINNED_OVERRIDE:+--override "$PINNED_OVERRIDE"} \
    > /tmp/update-self-target.json || exit 1
cat /tmp/update-self-target.json
REF=$(python3 -c 'import json; print(json.load(open("/tmp/update-self-target.json"))["ref"])')
```

`--local-tags` reads the tags the fetch just landed. To honor a user override
given in chat, set `PINNED_OVERRIDE` to it (`main`, `minds-v0.3.6`). The `||
exit 1` leaves a refusal's `error:` line as the last thing printed. The output
carries `ref`, `kind`, `ceiling` and `exceeds_ceiling`; an upstream branch name
such as `main` resolves to `upstream/main`.
Tell the user which version you are updating to, and never mention a release
above `ceiling` that they did not ask for by name: the Imbue Studio app announces its
own updates.

**If the command exits non-zero, stop.** Its single `error:` line says why no
target could be chosen. Relay it in plain terms, offer the next step, and
record the verdict it calls for -- never resolve a ref by hand:

- **Already on the release it may take.** Nothing is wrong with the workspace:
  `run-status verdict ALREADY_CURRENT`.
- **A fault**: the ceiling names a release the upstream does not carry, or no
  release at all, or, on a pass with no pin, the app could not be reached or is
  too old to report its version. A pinned pass never asks the app for its
  version, so an app it cannot reach is never a fault for it. Say so plainly and
  record `run-status verdict STUCK --detail "<the error line, in plain
  terms>"`. Do **not** pick another release; only a version the user names
  becomes an `--override`, as an operator testing a dev build would.
- **Anything else**: `run-status verdict REFUSED --detail "<the error line, in
  plain terms>"`.

**`"exceeds_ceiling": true`** means the user's `--override` names a version
this app cannot vouch for. Do not dispatch on it silently: tell them what it
risks and get an explicit go-ahead, unless the message that started this pass
already carries that confirmation (the Imbue Studio app's "Update to a specific
version" prompt says so). While you wait for their answer, record the wait, so
the app shows what the run is waiting on, and clear it once they answer:

```bash
python3 .agents/skills/update-self/scripts/update_self.py run-status hold \
    --detail "<one plain line: that you are waiting for them to confirm that version>"
```

```bash
python3 .agents/skills/update-self/scripts/update_self.py run-status resume
```

If they decline, record `run-status verdict REFUSED --detail "<the version they
asked for, and that they chose not to attempt it>"` and end the pass. Details
in `references/version-ceiling.md`.

To preview what the release changes, diff from the merge base (`git diff
--name-status "$(git merge-base HEAD "$REF")" "$REF"`), never from `HEAD`. A
workspace that predates the template's history rewrite has no merge base until
Step 3a bridges it, so its preview waits until then.

### 2a. Hand off to the target's own update-self flow

Stage the skill as it exists at `$REF` (from the fetched objects; no network,
no working-tree mutation) and learn whether it differs from your local copy:

```bash
python3 .agents/skills/update-self/scripts/update_self.py bootstrap-skill --ref "$REF" \
    > /tmp/update-self-bootstrap.json || exit 1
DIFFERS=$(python3 -c 'import json; print(json.load(open("/tmp/update-self-bootstrap.json"))["differs"])')
echo "differs=$DIFFERS"
```

**If it exits non-zero, stop.** Its `error:` line says `$REF` names no commit
here; handle it as a Step 2 error.

Otherwise `bootstrap-skill` has left a runnable flow at the staging path (the
target's copy, or the local copy when the ref predates the skill), so the
worker runs from there regardless. `differs` decides only which prose *you*
follow next:

- **`False`** -> continue with this document.
- **`True`** -> stop following this document and follow the staged copy's
  `SKILL.md` from **Step 3** onward
  (`data/.tasks/update-self/skill-at-target/.agents/skills/update-self/SKILL.md`).
  Steps 1-2 are done; do not re-run its Step 2 or re-stage -- carry `$REF`
  into its Step 3.

Steps 1-2 always run from the local copy and Step 3 onward from the target's;
when editing this skill, keep that boundary and the staging path stable. The
reasons, and what an edit must preserve, are in
`references/handoff-contract.md`.

## 3. Dispatch the worker

### 3a. Re-check the ceiling from the staged copy (first, before anything else)

```bash
PINNED_CEILING=""
python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py \
    resolve-target --local-tags --override "$REF" \
    ${PINNED_CEILING:+--app-version "$PINNED_CEILING"} > /tmp/update-self-recheck.json || exit 1
cat /tmp/update-self-recheck.json
```

Set `PINNED_CEILING` as Step 2 does: the `Ceiling:` of a message that pinned
the update, else empty, so a pinned pass never asks the app here either.

This is the only ceiling check that runs on a workspace updating *into* the
ceiling for the first time (its local copy may predate the check). If
`exceeds_ceiling` is `true` here, it matters who chose `$REF`.

**Step 2 chose it, without `--override`:** the user never asked for it. Do not
name it. Set `$REF` to the capped ref (re-run without `--override` to learn it,
keeping the pin; an error there is handled as in Step 2), tell the user that version is the one
you are updating to -- an initiator too old to know the ceiling has already
announced the other -- and **re-run §2a** before dispatching, so the staged copy
matches the target.

**The user named it:** take the confirmation now as in Step 2, offering the
capped ref, and record the wait with Step 2's `run-status hold` and `resume`,
run from the staged copy's `update_self.py`. If they take the capped ref, set
`$REF` to it and re-run §2a the same way. If they decline every option, record
`run-status verdict REFUSED --detail "..."`.

**Then bridge an old workspace to the target's history.** The template's
history was rewritten once, so a workspace created before that shares no commit
with `$REF` and the worker's merge would have no base. This grafts the
workspace's fork point onto its rewritten twin in `$REF` for the rest of this
pass; the workspace's own history is not touched, and for every other workspace
it changes nothing. Fetch the tags again first, forced: an older copy of Step 2
cannot move a tag this workspace already holds, and the bridge must be built
against the commit the worker will merge:

```bash
git fetch upstream --tags --force
python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py \
    bridge-history --ref "$REF" || exit 1
```

Either `"bridged"` value needs nothing from the user. If it exits non-zero, it
built no bridge: record `run-status verdict STUCK --detail "<the error line, in
plain terms>"`, surface it, and stop. From here on, a pass that ends for any
reason drops the bridge first (Step 6's opening command); a retry rebuilds it.

### 3b. Launch

Do not open a window for your own chat while the pass runs: Step 6 opens it
once the results are ready.

Open a tracking ticket (note the id it prints), then `tk start <ticket-id>` as
its own tool call:

```bash
mkdir -p data/.tasks/update-self
tk create "update-self" -t task \
    --acceptance "worker launched; conflicts triaged; validated; branch applied"
```

Write the task file: an **unquoted** frontmatter heredoc so `$MNGR_AGENT_ID`
and `$REF` expand, then a **quoted** body. The `lead_agent` line must stay:
this prose runs cross-version, and an older workspace's launcher may not stamp
it at launch. It is the lead's agent id, not its name: a rename of the lead's
chat mid-update would otherwise strand the worker's report.

```bash
{
cat << FRONTMATTER_EOF
---
lead_agent: $MNGR_AGENT_ID
finish_report_path: data/.tasks/update-self/reports/report.md
target_ref: $REF
---
FRONTMATTER_EOF
cat << 'BODY_EOF'

# Task: safe update-self

## What to do
Follow the worker guide at
`data/.tasks/update-self/skill-at-target/.agents/skills/update-self/references/update-self-worker.md`
end to end: trial-merge conflict triage, complete the merge (preserving the
`update-self:` merge-commit subject), validate the merged set, generate the
"what's new" report, and report `done`. That
`data/.tasks/update-self/skill-at-target/.agents/skills/update-self` path is the copy
of the update-self flow shipped with the version being updated to (staged by the
lead and synced into your worktree with this runtime dir) -- run *all* its
`update_self.py` calls from its `scripts/` too. Your target is the `target_ref` in
this file's frontmatter (already fetched into `upstream`).

## Reporting back
Per §6 of the worker guide: the report shapes come from
`.agents/shared/references/worker-reporting.md`, but you write and push the
report by hand as §6 spells out -- not with the launcher's `report` subcommand,
which this workspace's own launcher may predate. Valid `name:` values:
`question` (mid-flight gate: a genuine, unresolvable conflict, the §4a/§4b/§4c
scope escape hatch, or a §4b customization the update cannot keep),
`done` / `stuck` (terminal). `<TASK_FILE>` -> `data/.tasks/update-self/task.md`.
BODY_EOF
} > data/.tasks/update-self/task.md
```

Clear the previous pass's worker, which Step 6 leaves *stopped* (its transcript
stays reachable for bug reports). A worker of that name in state `STOPPED` or
`DONE` is destroyed; one in any other state is still running -- a genuine
conflict, resolved per the lease check in Step 1, never forced past. Plain
`mngr` and `git` commands on purpose: this prose runs from the target's copy
but launches with the workspace's own, possibly older, `create_worker.py`
(`scripts/launcher_contract_test.py` pins what it may ask of it):

```bash
mngr list --format "{name}	{state}" 2>/dev/null | grep -P "^update-self\t"
```

```bash
mngr destroy update-self --force
```

Then clear the previous pass's `mngr/update-self` branch, whether or not a
worker was listed: destroying a worker leaves its branch behind, and the launch
below cannot create the worker while a branch of that name exists. First ask
whether `HEAD` already has it -- exit 0 yes, 1 no, and a `Not a valid object
name` error means there is no such branch and nothing to clear:

```bash
git merge-base --is-ancestor refs/heads/mngr/update-self HEAD
```

After a landed pass it does (a rollback is a revert commit on top of the
merge), so delete it. `-D`, because that check is the safety check: `-d` asks
the branch's pushed copy instead of `HEAD` once GitHub sync has pushed it:

```bash
git branch -D mngr/update-self
```

Exit 1 means the branch holds commits `HEAD` does not have. Keep them under an
archive name instead (note the name it prints). The results message then
carries a plain caveat that unfinished work from an earlier update attempt was
set aside and kept, and can be recovered on request; the archive name itself
goes in the tracking ticket's close summary, not the message:

```bash
ARCHIVE="archive/update-self-$(date +%Y%m%d-%H%M%S)"
git branch -m mngr/update-self "$ARCHIVE" && echo "$ARCHIVE"
```

Launch with the plain `worker` template, record the hand-off (from here until
the worker reports, this chat has ended its turn but is busy on the poll below,
so the Imbue Studio app reads it as updating rather than "waiting for you";
naming the worker lets the app read the worker's liveness as well), then
background-poll through `run_in_background.py`, which delivers `await`'s result
to this chat as a message that starts your next turn, whatever your harness. Run
the staged skill's copy of it: this workspace's own tree may predate the script.

```bash
uv run .agents/skills/launch-task/scripts/create_worker.py launch \
    --name update-self --template worker \
    --runtime-dir data/.tasks/update-self/ --task-file data/.tasks/update-self/task.md
```

```bash
python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py \
    run-status delegate update-self
```

```bash
python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/run_in_background.py \
    --description "Wait for the update's background agent" -- \
    uv run .agents/skills/launch-task/scripts/create_worker.py await \
    --name update-self --task-file data/.tasks/update-self/task.md --timeout 90m
```

Once the poll is armed, **end your turn**; its message wakes you with the
report. Re-arm with the same command. Never wait on the worker any other way
-- no `sleep`, no polling its reports directory or its pane -- see "Never sleep
on a worker" in `.agents/shared/references/lead-proxy.md`.

## 4. Proxy the `question` gate

Per `.agents/shared/references/lead-proxy.md` (worker `update-self`, branch
`mngr/update-self`, reports dir `data/.tasks/update-self/reports/`). A
`question` is one of four things; you answer the first three yourself, and only
the last reaches the user. For the first three: reply via `create_worker.py
reply --task-file data/.tasks/update-self/task.md -m "..."`, consume the
report, re-arm the poll. The last ends the pass, as below.

1. **A genuine, unresolvable merge conflict.** Decide it yourself. The default
   is to **keep this workspace's current behavior**: preserve the local
   customization, fold in what upstream adds around it, and take the release's
   side only where no local intent is at stake. Record every decision (file,
   what was kept, what the release's version would have changed) -- the
   results message presents each with the alternative still on offer. A
   conflict where *every* resolution breaks something the user built is not
   a merge question; it is the hold below.
2. **The worker's scope escape hatch** (its §4a, §4b or §4c): a process
   question about whether its impact analysis runs, whether a validation item
   runs, or whether and at what scope the review gates run. Answer it by the
   rule it names as written; where the rule is silent, the fallback is more
   coverage, never less. A `select-tests` output is not silence: what it
   printed for a commit is that commit's whole gate. Escalate only if it
   contains a real question of user intent.
3. **Its gate was shed for memory**, and it is asking for room to rerun. Free
   it yourself per `references/freeing-memory-for-an-update.md`, which says
   what this pass may stop and how much further a scheduled run may go, then
   reply with what you stopped and the free memory the candidates command
   reports *after* it. Memory pressure is never a reason to narrow the gate, so
   never answer it by telling the worker to skip or trim one. It reaches the
   user only when the licence is spent and the room is still not there: then
   ask them, recording the wait with Step 2's `run-status hold` and clearing it
   with `resume`, and name what is left to stop and what stopping it costs
   them. The worker keeps its branch and waits; this is not the §4b hold.
4. **A customization hold** (its §4b verdict): something the user built that
   the update **cannot keep**, after the worker genuinely tried to re-fit it.
   This is the one gate that reaches the user, and it ends the pass; see
   below. A cosmetic shift (the widget moved but still works) is not a hold:
   it applies unattended and is named in the results message with an offer
   to restore.

**A hold ends the pass with a decision for the user.** Nothing waits for their
answer holding a lease or a live worker, however long they take. In this order,
each command on its own:

1. Record the verdict. The detail line is shown in the app, so write it for
   the user:

   ```bash
   python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py \
       run-status verdict NEEDS_DECISION \
       --detail "<one plain line: what they built that the update cannot keep>"
   ```

2. Stop the worker, keeping it. Its branch `mngr/update-self`, its worktree and
   its conversation are what re-land the update once they decide, so never
   destroy it or delete the branch here:

   ```bash
   mngr stop update-self
   ```

   Then consume the hold's report, so the poll re-armed when they answer waits
   for the worker's reply (an older launcher's `await` leaves it in place, and
   would return it at once):

   ```bash
   mkdir -p data/.tasks/update-self/reports/consumed
   [ ! -e data/.tasks/update-self/reports/report.md ] || mv data/.tasks/update-self/reports/report.md \
       data/.tasks/update-self/reports/consumed/$(date +%s)-question.md
   ```

3. Run Step 6's opening commands (they drop the history bridge and bring back
   any chat this pass paused), then release the `updating workspace` lease:
   `tk close "$UPDATE_LEASE_ID" "Waiting on the user's decision."`. Leave the
   tracking ticket open; the re-land closes it.

4. Compose the decision message from their point of view per
   `references/results-message.md` ("The decision message"), open this chat's
   window as Step 6 ends, and end your turn.

Their answer, whenever it comes, arrives in this chat: follow
`references/resuming-after-a-decision.md`.

## 5. Terminal status

- A worker that died without reporting is not yet a failure: recover it per
  `.agents/skills/launch-task/references/dead-worker-recovery.md`, then re-arm
  the poll. It is stuck only once that recovery has not brought it back.
- A **`stuck`** report that shows the merge cannot land without breaking the
  running workspace is a migration-required update: follow
  `references/apply-outcomes.md`, "Migration-required updates", which ends the
  pass `NEEDS_RECREATION`.
- Any other **`stuck`**, or a dead worker its recovery did not bring back -> surface per
  `.agents/skills/launch-task/references/worker-failure.md` (if the user
  then overrides, see `references/resuming-after-stuck.md`). Nothing is merged
  or applied. Compose it as a plain-language lead ("I couldn't complete this
  update cleanly; your workspace is untouched") followed by a clearly-marked
  technical block with the specifics **verbatim** (target ref, failing step,
  the error text, a pointer to `data/.tasks/update-self/reports/`) -- this is
  the one message where detail is preserved, because it gets pasted into bug
  reports. Record `run-status verdict STUCK --detail "<one plain line on what
  failed>"`, and drop the history bridge as Step 6 opens.
- **`done`** -> the audit below.

### 5a. Audit the report

The worker contract (the staged copy's `references/update-self-worker.md`,
§4a, §4b, §4c and §6) makes the impact analysis, the validation scope and the
review gates rule-driven and the report evidence-bearing. It must show which
branch of the 4a and 4b rules applied (the footprint evidence --
`has_local_footprint`, and per creation the scope files' verdicts when it is
true -- and each validation item's condition and whether it held), and either
show the clean-pull skip's three conditions held (`has_merge_work: false`, no
impacted user-created code, no worker-authored in-branch edits beyond Step 1's
rollback reverts as git made them or with a `both added` file taken at the
target's version, shown by an empty diff against the landed merge) or carry
the gate run's own evidence (fix commits kept or reverted, or a clean run,
plus architecture-gate verdicts); a side-picked conflict must carry the
discarded-side accounting. A report missing any of this -- including one that
openly discloses skipping a gate outside the rule -- goes back to the worker
via the Step 4 cycle (say what is missing, consume the report into
`data/.tasks/update-self/reports/consumed/`, re-arm). Do not run the apply
over the gap. A deviation stands only when the worker is gone and the gap
cannot be closed from here, and then the results message states it plainly as
a caveat.

A `done` that answers a catch-up (5b) carries a **Catch-up** section, and the
audit reads it the same way: it must show which of the worker guide's §7 items
ran over the commits it brought in, each with its evidence, and how each
conflict there was resolved. The worker's earlier evidence for the rest of the
pass still stands. The clean-pull skip's empty diff then runs to the branch's
tip before its first catch-up merge, not to its tip
(`references/worker-review-gates.md`).

There is no approval gate: the audit, not the user, authorizes the apply. The
`done` report is your raw material, not the user's message; the results
message is composed *after* the apply, per `references/results-message.md`.

### 5b. Apply the update (one atomic motion)

**Rebuild-only findings do not block the apply**, and neither does a
user-created dependent the worker could not verify; they become leading
caveats of the results message (a global-dependency bump coupled to a
user-created dependent: name it, check it after the apply, offer rollback or a
workspace recreate; a
container build/launch parameter a running container cannot adopt: say it
stays inert until a recreate). A genuinely breaking case takes the migration
path below instead.

**When the update touches a critical app (`system/apps/system_interface/`,
`system/apps/chat/`, `system/apps/terminal/`, `system/apps/terminal_pty/`),
`system/apps/getting_started/frontend/`, `system/apps/files/frontend/`,
`system/apps/routines/frontend/`, `system/libs/workspace_ui/`,
`system/libs/workspace_layout/`, or `system/package.json` /
`system/package-lock.json` at all** (every critical app's tree, plus the
Getting Started, Files and Routines frontends, the shared library,
and the npm files, whose change rebuilds every frontend bundle, and the layout
wire contract the critical apps run), also take the `editing
critical app <name>` lease for each critical app it touches through the apply,
as `update-app/references/critical-app.md` does (`<name>` is the app's
`app.toml` name, so `system/apps/terminal_pty/` is `terminal-pty`; the Getting
Started, Files and Routines frontends, `workspace_ui`, and the npm files count as both
`system_interface` and `chat`; `workspace_layout` counts as `system_interface`,
`chat`, and `terminal`). Take them all or none, as that reference says:
check each one in `tk ready` (`grep -E -- "- editing critical app <name>$"`,
anchored so `terminal` does not match `terminal-pty`'s lease), take them in
name order (`tk create "editing
critical app <name>" -t chore`, then `tk start` it, each as its own command),
and if any is held by another agent, release the ones you took and surface it
instead of proceeding. Take them right before the apply, once the chats are
paused and the last fast-forward check below has passed, so no catch-up round
runs while you hold them, and release them afterwards.

The apply run from here keeps its own run record and raises no "recently
updated" notice: `--keep-rollback-point` is the careful flow's, not this one's.

**Before the apply, bring the worker up to date, then pause the other chats.**
Other chats keep working while the worker runs, and nothing waits on them:
they are paused only now, right before the apply, for as long as it takes.

First, while they still run, check whether they committed since the worker
branched:

```bash
git merge-base --is-ancestor HEAD mngr/update-self
```

Exit 0 means the branch still fast-forwards. Exit 1 means `HEAD` moved under
the pass, and the apply would refuse it: **catch the worker up** instead of
redoing the pass. Consume the `done` you are answering first (an older
launcher's `await` leaves it in place, and a re-armed poll would return it at
once), then message the worker (it is still waiting, and holds the branch and
any bundles it built), re-arm the poll as in Step 3b, and audit the `done` it
sends back (5a) before you come back here. The workspace's own launcher may
predate a reply subcommand, so send through the chat app's `message_chat.py`
where the workspace has it, addressed by the agent id the launcher stamped into
the task file, and otherwise through `mngr message`. Either one starts a worker
that stopped.

```bash
mkdir -p data/.tasks/update-self/reports/consumed
[ ! -e data/.tasks/update-self/reports/report.md ] || mv data/.tasks/update-self/reports/report.md \
    data/.tasks/update-self/reports/consumed/$(date +%s)-done.md
```

```bash
CATCH_UP="Catch up to $(git rev-parse HEAD) per §7 of your worker guide, then report done again."
WORKER_ID=$(sed -n 's/^worker_agent_id: *//p' data/.tasks/update-self/task.md)
if [ -f system/scripts/message_chat.py ] && [ -n "$WORKER_ID" ]; then
    python3 system/scripts/message_chat.py "$WORKER_ID" -m "$CATCH_UP"
else
    mngr message "${WORKER_ID:-update-self}" --start -m "$CATCH_UP"
fi
```

A non-zero exit other than 7, or a `No agents found` line, means the message
did not reach the worker: do not re-arm the poll on it. Exit 8 means the
worker is gone, and takes the fresh pass below. Otherwise check whether the
worker is alive ("Diagnose worker liveness" in
`.agents/shared/references/lead-proxy.md`) and send again; a worker that is
gone takes the fresh pass below. Exit 7 means it arrived but the worker sits
on a dialog it could not dismiss: do not send it again; re-arm the poll, and
diagnose liveness the same way if no report comes.

A moved `HEAD` never calls for a fresh pass, so never destroy or stop the
worker to make room for one: after a moved `HEAD`, a fresh pass (Step 3b) is
only for a worker that is gone, or one that reports `stuck` on the catch-up
and is not resumed (`references/resuming-after-stuck.md`).

Once the branch fast-forwards, **pause** every chat and background agent that
is busy (mid-turn, or waiting on a background command), other than this chat
and the update's own worker and anything under it. It records each one, so the
resume below starts it again with a note:

```bash
python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py \
    run-status stop-chats
```

It prints a line per agent it paused; keep them for the results message. A
non-zero exit names what it could not pause and why: a listing that failed
paused nothing, so run it once more; an agent that would not stop keeps
running, which only means its edits may land beside the apply, as they always
could. Neither holds the apply back.

Then **save what the chats left uncommitted**, as Step 1 does: one commit on
the workspace's branch listing the paths. Note the sha it prints for the resume
below:

```bash
git add -A
{ echo "Save work in progress before updating to $REF"; echo
  echo "Uncommitted changes, saved as they stood so the update could apply:"
  git diff --cached --name-only | sed 's/^/- /'; } > /tmp/update-self-wip-message.txt
git diff --cached --quiet || { git commit -F /tmp/update-self-wip-message.txt && git rev-parse HEAD; }
```

An `editing service <name>` lease another chat holds is no reason to wait: that
chat is paused now, and its edit is in this commit. (An `editing critical app`
lease is not one either: that flow edits in its own worktree, never the served
tree, and the lease check above covers the critical apps this update touches.)

A commit here, or one a chat made just before it paused, moves `HEAD` again:
run the fast-forward check once more, and on exit 1 catch the worker up as
above. The chats stay paused through that round, which covers only those
commits.

Run the apply from the staged copy, in the **foreground**: its output (refusal
and resume messages, any provisioner warning, the `apply phase timings:` line)
is what you read before recording a verdict.

```bash
python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py apply \
    --merge-ref mngr/update-self --ff-only --target-ref "$REF"
```

When the report names the worker's **built frontend bundles** (the shell's
`static/`, the chat app's, the Getting Started app's, the Files app's, and the Routines app's), append
`--worker-bundle system_interface=<path> --worker-bundle chat=<path>
--worker-bundle getting_started=<path> --worker-bundle files=<path> --worker-bundle routines=<path>` so the
exact builds the worker validated
are installed instead of a live build; the apply installs them only as a set (one
`npm run build` emits them all), and builds live when any is missing or stale.

That one command is the whole landing: it fast-forwards to the worker's
branch, refreshes, rebuilds and restarts what the update changed, probes the
workspace's health, and reverts the entire merge on any failure. Exit codes,
each spelled out in `references/apply-outcomes.md`:

- **`0` -- applied.** Read its closing stderr lines before signing off.
- **`2` -- automatically rolled back.** The update did not land and the
  workspace is healthy on the previous revision. You judge whether to retry
  ("Exit 2" has the rules), and a retry goes back to the worker. When your best
  effort is spent, record `run-status verdict REFUSED --detail "<what failed,
  one plain line>"` at once.
- **`3` -- emergency.** Even the rollback could not restore health: escalate,
  and record `run-status verdict STUCK --detail "<what failed, one plain
  line>"`.
- **`1` -- precondition; nothing changed.** A dirty tree or a moved `HEAD`
  means something wrote after the pause: the chats stay paused while you
  release the `editing critical app` leases you took, save the new changes the
  same way, and come back through the fast-forward check. Anything else that
  makes you wait (another apply in flight, a merge that goes back to the worker
  to undo a rollback) can take far longer than an apply, so bring the chats
  back first (`run-status resume-chats`, without `--landed`, and with
  `--wip-commit <sha>` when 5b's save made a commit) and let the next 5b pause
  them again.

**Right after an apply that exits 0, 2 or 3, bring back what you paused**,
naming the release when it landed (exit 0) and the commit that saved the
uncommitted work, when there was one:

```bash
python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py \
    run-status resume-chats --landed "$REF" --wip-commit <sha>
```

Leave `--landed` out after exit 2 or 3, and `--wip-commit` when nothing was
saved. Each chat is started again and told what changed while it was paused,
in a notice the user sees labelled "Workspace updated"; a background agent is
named to its lead chat, since it does not pick its work back up by itself. It
is safe to re-run: a non-zero exit names what it could not start or tell, and
the same command finishes the rest.

What each outcome means for the user, the `provision-incomplete` and
`emergency.json` records, an interrupted apply (re-run the same command; it
resumes), and how to honor a rollback request are in
`references/apply-outcomes.md`.

### 5c. Carry out the report-driven remainder

- **`shared_runtime` live consumers** -- the apply's restart has already put
  every built-in service on the merged code. What the report can still name is
  a *user-created* consumer (an app or skill of theirs reading something the
  update changed): check it, and carry any breakage into the results message.
- **`Dockerfile`** -- apply the live-applicable hunks the report calls out
  (version pins live in `setup_system.sh`, already re-run; keep
  `agent_types.claude.version` in `.mngr/settings.toml` in sync). An
  image-level hunk the report marks *pending recreate* is a results-message
  caveat naming what the workspace lacks until a recreate; one marked
  *covered live* changes nothing a recreate would fix, so leave it out.
- **Rebuild-only flags** -- surface each as needing a workspace recreate,
  saying what the running workspace lacks until then; never imply they are
  live.
- **User apps without `stop_when_no_windows`** -- the shell stops an app
  whose manifest declares `stop_when_no_windows = true` a minute after its
  last window closes and starts it again on the next request (the build-app
  skill says which apps should carry it). A manifest without the field reads
  as `false`, so an app built before the field existed keeps running for the
  life of the workspace, as it always did. Do not add the field
  mechanically. For each `system/apps/*/app.toml` that is the user's and
  lacks it, read the app: if it only answers requests, add
  `stop_when_no_windows = true` and restart its program so the registration
  carries it; if it does work between requests (a background thread, a
  poller or scheduled refresh, a subscription to an outside service, a job
  that outlives the window, an API another agent drives with no window),
  add `stop_when_no_windows = false` with a comment saying why, so the next
  pass does not ask again. Name each decision in the results message.

### 5d. Escalate the built-in defects this pass found

Before composing the results message, collect every finding the worker labelled
a `submit-upstream-changes` candidate, plus any other defect in built-in code
you hit this pass (a failing built-in test, a built-in test the worker's
Validation names as flaky, a step of this flow that broke and had to be worked
around). Escalate them together as AGENTS.md's "Updates"
section describes -- one report via
`.agents/shared/references/report-built-in-issues.md`, or
`submit-upstream-changes` for a template fix -- or name each in the results
message with the submission offered. A flaky test of a creation the workspace
built is not built-in: file a regular ticket for it (`tk create`).

Then compose the results message per `references/results-message.md`.

## Outside the main flow

Each of these has its own reference, read only in its situation:

- The user overrides a `stuck` verdict: `references/resuming-after-stuck.md`.
- The user answers a decision this pass ended on (Step 4):
  `references/resuming-after-a-decision.md`.
- A note from the Imbue Studio app says this chat stopped and was started
  again: `references/resuming-after-a-restart.md`.
- The user tells you to stop or cancel the update:
  `references/stopping-when-asked.md`.
- The update cannot be applied in place: `references/apply-outcomes.md`,
  "Migration-required updates".

## 6. Teardown

Whatever the outcome, a needs-decision ending included, drop the history
bridge (a no-op when Step 3a built none; a retry rebuilds it) and bring back
every chat and background agent this pass paused, or an earlier pass paused and
ended without bringing back (a no-op when 5b's resume already did, or nothing
was paused):

```bash
python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py \
    bridge-history --drop
```

```bash
python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py \
    run-status resume-chats
```

After an apply that landed, give this resume 5b's `--landed` and
`--wip-commit` too, so a chat it still had to tell hears the same news.

Then put back the apps and browsers this pass stopped for memory, which no
command records: an apply that landed has already restarted every program, so
this is for every other ending
(`references/freeing-memory-for-an-update.md`, "Putting it back").

If a stray preview of a critical app is registered (an older pass may have left
one; the careful flow refuses its next pass on that app while one is), tear it
down with the preview script and close its window:

```bash
uv run --no-sync workspace-layout close <name>-preview
uv run python3 .agents/skills/update-app/scripts/preview_app.py down --app <name>
```

The close goes first: nothing takes a window away when its app leaves the registry, so
once `down` has deregistered the row the preview's window would stay on the desktop
pointing at a page nothing serves.

**The rest is only for a successful apply (exit 0).** After a rollback the
worker's branch, worktree and report are what a retry starts from: keep them
until 5b's retry judgment is spent, but release the leases either way.

Record the success verdict -- `UPDATED`, or `UPDATED_WITH_REBUILD_ITEMS` when
§5c left something the user must actually do or decide. A purely-deferred item
with nothing observable to act on (a pending-recreate item whose difference
the user would not notice) is at most a results-message caveat under a
plain `UPDATED`, not a reason for this verdict -- the app renders it as "left
something for you", and there must really be something. When you do record it,
the `--detail` line names that something in the user's terms:

```bash
python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py \
    run-status verdict UPDATED \
    --resulting-ref "$REF" --detail "<one plain line for the app's modal>"
```

Consume the report (a leftover one would satisfy the next pass's `await`
instantly, and `launch` refuses over it) and **stop** the worker rather than
destroying it, so its transcript stays reachable for bug reports:

```bash
mkdir -p data/.tasks/update-self/reports/consumed
mv data/.tasks/update-self/reports/report.md \
    data/.tasks/update-self/reports/consumed/$(date +%s)-done.md
mngr stop update-self
```

Release the leases and close the ticket last, each as its own tool call: `tk
close` each `editing critical app <name>` lease 5b took, then the
`updating workspace` lease (`tk close "$UPDATE_LEASE_ID" "Update pass
finished."`), then `tk close <ticket-id> "Updated to <ref> -- worker branch
merged and applied."`, adding the `archive/update-self-<timestamp>` name when
Step 3b set a previous branch aside.

**Last, whatever the outcome, show the user this chat** once its results (or
its decision, or its `stuck` account) are ready to read, then end your turn
with that message:

```bash
CHAT_ID="${MINDS_CHAT_ID:-$MNGR_AGENT_ID}"
uv run --no-sync workspace-layout show chat --path "/?chat=$CHAT_ID" --showing "/$CHAT_ID" --repoint /
```

`show` raises a window already on this chat, else points a chat window on
screen at it, and opens one only when there is none.

If the command fails (a tree from before it, a shell that is not up), carry on:
the app's update row still leads the user here.

## To push local improvements back upstream

Use the `submit-upstream-changes` skill -- the complementary direction. This
skill only pulls.

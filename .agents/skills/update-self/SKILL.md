---
name: update-self
description: Safely pull updates from the upstream template repo (default target is the latest stable release the running Minds app supports). Use when you want to incorporate upstream skills, script fixes, or config improvements. For pushing local improvements back upstream, use the `submit-upstream-changes` skill instead.
metadata:
  author: imbue
---

# Pulling updates from the upstream template, safely

This repo was created from a template repo and stays connected to it via a git
remote (`system/config/parent.toml` has the URL and branch). Upstream carries
the shared infrastructure: skills, scripts, `CLAUDE.md` scaffolding,
`Dockerfile`, `system/supervisord.conf`, the system interface, the vendored
`mngr`.

Merging upstream can break the live workspace, so this flow never mutates the
live tree from an unverified state: an isolated **worker** does the merge and
validation on its own branch, and only a validated result is landed by one
atomic, rollback-on-failure **apply**.

You are the **lead**, and the pass is **fully unattended**: resolve the target,
dispatch the worker, answer its gates, audit its evidence, run the apply, then
report. The user launches an update and walks away; they come back to a
finished result and an offer to roll back anything they don't like. Two things
still wait for the user: an `--override` past the version ceiling (asked at
launch, while they are present) and an update that cannot keep something they
built (the Step 4 hold).

The default target is the **latest stable `minds-v*` tag**, never newer than
the Minds app driving this workspace (the template ships the code that app
talks to); see `references/version-ceiling.md`. Once the target is resolved,
the pass **re-points itself at the target version's own copy of this skill**
(Step 2a) and runs the rest -- lead and worker -- from the fixed staging path
`data/.tasks/update-self/skill-at-target/.agents/skills/update-self`, so fixes
to the update flow that shipped in the release are applied on the way in.
Address that path by literal each time (each bash invocation is a fresh shell).

## 1. Preconditions

**Back up first.** Capture a restore point of the whole workspace; it is the
last-resort recovery path if the apply's own rollback and `recover` both fail:

```bash
uv run host-backup-now
```

Exit 0 means `restic_backup_succeeded`; 3 (not configured), 1 (failed) and 2
(outcome not observable) mean there is **no** confirmed restore point. **None
of them blocks the pass**: note which it was, carry it into the results message
as a caveat, and continue -- git still holds every version of the tree. Do not
stop to ask for a go-ahead.

**Take the "updating workspace" lease.** One update flow at a time (worker
name, branch and runtime dir are fixed, and two applies must never interleave).
Check for a foreign one first:

```bash
tk ready > /tmp/update-self-inflight.txt
grep "updating workspace" /tmp/update-self-inflight.txt
```

If another agent holds a live `updating workspace` lease, stop and tell the
user; if it looks abandoned, take it over per
`.agents/shared/references/harden-contention.md`. Otherwise take it (each `tk`
call as its own command, never chained):

```bash
UPDATE_LEASE_ID=$(tk create "updating workspace" -t chore \
    -d "Held by $MNGR_AGENT_NAME across the update-self pass; released at teardown.")
```

then `tk start "$UPDATE_LEASE_ID"`.

**Record the run for the Minds app** -- as soon as the lease is yours, so the
app can see a run is under way:

```bash
python3 .agents/skills/update-self/scripts/update_self.py run-status start
```

This writes `data/.state/update-apply/run.json`, which the app polls. Besides
the `hold`/`resume` pair around its one mid-flight question (Step 4), the pass
owes that file exactly one more write, a `run-status verdict` when it ends;
every terminal path below names its verdict, and a pass that ends without one
shows the user "update failed". It comes after the lease check because there
is one record per workspace, and a pass that stops because someone else is
updating must leave the running pass's record alone.

**Clean tree.** The worker branches off your `HEAD` and the rollback captures
it. If `git status --porcelain` is non-empty, record `run-status verdict
REFUSED --detail "<what is uncommitted, one plain line>"`, surface it, and
stop.

## 2. Resolve the target

Ensure the remote exists, heal a shallow clone, fetch with tags, and resolve:

```bash
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
git fetch upstream --tags

python3 .agents/skills/update-self/scripts/update_self.py resolve-target --local-tags \
    > /tmp/update-self-target.json || exit 1
cat /tmp/update-self-target.json
REF=$(python3 -c 'import json; print(json.load(open("/tmp/update-self-target.json"))["ref"])')
```

`--local-tags` reads the tags the fetch just landed. To honor a user override,
append `--override main` or `--override minds-v0.3.6`. The `|| exit 1` leaves a
refusal's `error:` line as the last thing printed. The output carries `ref`,
`kind`, `ceiling`, `exceeds_ceiling`, `latest_available` and
`held_back_by_ceiling`; `main` resolves to `upstream/main`. Tell the user which
version you are updating to.

**If the command exits non-zero, stop -- nothing is wrong with the workspace.**
Its single `error:` line says why no target could be chosen (the Minds app
could not be reached or is too old to report its version; every release is
newer than the app; the workspace is already on the release it may take).
Relay that line in plain terms and offer the next step; never resolve a ref by
hand. Record the verdict first: `run-status verdict ALREADY_CURRENT` when the
error says the workspace is current, else `run-status verdict REFUSED --detail
"<the error line, in plain terms>"` (with `--in-place-compatible-ref` when the
error names a release the workspace could still take).

**`"exceeds_ceiling": true`** means the user's `--override` names a version
this app cannot vouch for. Do not dispatch on it silently: tell them what it
risks and get an explicit go-ahead, unless the message that started this pass
already carries that confirmation (the Minds app's "Update to a specific
version" prompt says so). If they decline, record `run-status verdict REFUSED
--detail "<the version they asked for, and that they chose not to attempt
it>"` and end the pass. Details in `references/version-ceiling.md`.

To preview what the release changes, diff from the merge base (`git diff
--name-status "$(git merge-base HEAD "$REF")" "$REF"`), never from `HEAD`.

### 2a. Hand off to the target's own update-self flow

Stage the skill as it exists at `$REF` (from the fetched objects; no network,
no working-tree mutation) and learn whether it differs from your local copy:

```bash
DIFFERS=$(python3 .agents/skills/update-self/scripts/update_self.py bootstrap-skill --ref "$REF" \
    | python3 -c 'import sys, json; print(json.load(sys.stdin)["differs"])')
echo "differs=$DIFFERS"
```

`bootstrap-skill` always leaves a runnable flow at the staging path (the
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
python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py \
    resolve-target --local-tags --override "$REF" > /tmp/update-self-recheck.json || exit 1
cat /tmp/update-self-recheck.json
```

This is the only ceiling check that runs on a workspace updating *into* the
ceiling for the first time (its local copy may predate the check). If
`exceeds_ceiling` is `true` here and the user has not already confirmed an
over-ceiling override, take that confirmation now as in Step 2, offering the
capped ref (re-run without `--override` to learn it). If they take the capped
ref, set `$REF` to it and **re-run §2a** before dispatching (the staged copy
must match the target). If they decline every option, record `run-status
verdict REFUSED --detail "..."` as in Step 2.

### 3b. Launch

Surface your own chat tab first (the Minds app sends the user into this
workspace when it starts an update, and this conversation is where they should
land). The command detaches a helper that retries until a client is there; it
is best-effort, and a failure is not a reason to stop:

```bash
python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py \
    surface-chat-tab --name "$MNGR_AGENT_NAME"
```

Open a tracking ticket (note the id it prints), then `tk start <ticket-id>` as
its own tool call:

```bash
mkdir -p data/.tasks/update-self
tk create "update-self" -t task \
    --acceptance "worker launched; conflicts triaged; validated; branch applied"
```

Write the task file: an **unquoted** frontmatter heredoc so `$MNGR_AGENT_NAME`
and `$REF` expand, then a **quoted** body. The `lead_agent` line must stay:
this prose runs cross-version, and an older workspace's launcher may not stamp
it at launch.

```bash
{
cat << FRONTMATTER_EOF
---
lead_agent: $MNGR_AGENT_NAME
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
Per `.agents/shared/references/worker-reporting.md`. Valid `name:` values:
`question` (mid-flight gate: a genuine, unresolvable conflict, the §4c
review-gate escape hatch, or a §4b customization the update cannot keep),
`done` / `stuck` (terminal). Substitutions:
`<TASK_FILE_GLOB>` -> `data/.tasks/update-self/task.md`;
`<RUNTIME_REPORTS_DIR>` -> `data/.tasks/update-self/reports`.
BODY_EOF
} > data/.tasks/update-self/task.md
```

Clear the previous pass's worker, which Step 6 leaves *stopped* (its transcript
stays reachable for bug reports). A worker of that name in state `STOPPED` or
`DONE` is destroyed (its `mngr/update-self` branch survives); one in any other
state is still running -- a genuine conflict, resolved per the lease check in
Step 1, never forced past. Plain `mngr` commands on purpose: this prose runs
from the target's copy but launches with the workspace's own, possibly older,
`create_worker.py` (`scripts/launcher_contract_test.py` pins what it may ask
of it):

```bash
mngr list --format "{name}	{state}" 2>/dev/null | grep -P "^update-self\t"
```

```bash
mngr destroy update-self --force
```

Launch with the plain `worker` template, record the hand-off (from here until
the worker reports this chat is idle, and naming the worker lets the Minds app
read the worker's liveness instead of "waiting for you"), then background-poll:

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
uv run .agents/skills/launch-task/scripts/create_worker.py await \
    --name update-self --task-file data/.tasks/update-self/task.md --timeout 90m
```

## 4. Proxy the `question` gate

Per `.agents/shared/references/lead-proxy.md` (worker `update-self`, branch
`mngr/update-self`, reports dir `data/.tasks/update-self/reports/`). A
`question` is one of three things; you answer the first two yourself, and only
the third reaches the user. Either way: reply via `mngr message`, consume the
report, re-arm the poll.

1. **A genuine, unresolvable merge conflict.** Decide it yourself. The default
   is to **keep this workspace's current behavior**: preserve the local
   customization, fold in what upstream adds around it, and take the release's
   side only where no local intent is at stake. Record every decision (file,
   what was kept, what the release's version would have changed) -- the
   results message presents each with the alternative still on offer. A
   conflict where *every* resolution breaks something the user built is not
   a merge question; it is the hold below.
2. **The worker's review-gate escape hatch** (its §4c): a process question
   about whether or at what scope the gates run. Answer it by the §4c rule as
   written; where the rule is silent, the fallback is more coverage, never
   less. Escalate only if it contains a real question of user intent.
3. **A customization hold** (its §4b verdict): something the user built that
   the update **cannot keep**, after the worker genuinely tried to re-fit it.
   This is the one gate that reaches the user; see below. A cosmetic shift
   (the widget moved but still works) is not a hold: it applies unattended
   and is named in the results message with an offer to restore.

For the hold, record it first, so the app can say what the machine is waiting
on (the detail line is shown in the app's modal, so write it for the user):

```bash
python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py \
    run-status hold \
    --detail "<one plain line: what they built that the update cannot keep>"
```

Then compose the question from their point of view per
`references/results-message.md` (what they built, what the update does to it,
the evidence, the choices with a recommendation, and that nothing has been
applied) and **wait**: the pass holds, worker alive and leases held, until they
answer. When they do:

```bash
python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py \
    run-status resume
```

If they skip the update, record `run-status verdict REFUSED --detail "<what the
update could not keep, and that they chose to keep it instead>"` and tear down;
without it the app reports a run that stalled for no reason. The other answers
carry on into §5 and get their verdict there.

## 5. Terminal status

- **`stuck`** or a dead-worker timeout -> surface per
  `.agents/skills/launch-task/references/worker-failure.md`. Nothing is merged
  or applied. Compose it as a plain-language lead ("I couldn't complete this
  update cleanly; your workspace is untouched") followed by a clearly-marked
  technical block with the specifics **verbatim** (target ref, failing step,
  the error text, a pointer to `data/.tasks/update-self/reports/`) -- this is
  the one message where detail is preserved, because it gets pasted into bug
  reports. Record `run-status verdict STUCK --detail "<one plain line on what
  failed>"`.
- **`done`** -> the audit below.

### 5a. Audit the report

The worker contract (the staged copy's `references/update-self-worker.md`,
§4c and §6) makes the review gates rule-driven and the report evidence-bearing.
It must either show the clean-pull skip's three conditions held
(`has_merge_work: false`, no impacted user-created code, no worker-authored
in-branch edits) or carry the gate run's own evidence (fix commits kept or
reverted, or a clean run, plus architecture-gate verdicts); a side-picked
conflict must carry the discarded-side accounting. A report missing any of
this -- including one that openly discloses skipping a gate outside the rule
-- goes back to the worker via the Step 4 cycle (say what is missing, consume
the report into `data/.tasks/update-self/reports/consumed/`, re-arm). Do not
run the apply over the gap. A deviation stands only when the worker is gone
and the gap cannot be closed from here, and then the results message states
it plainly as a caveat.

There is no approval gate: the audit, not the user, authorizes the apply. The
`done` report is your raw material, not the user's message; the results
message is composed *after* the apply, per `references/results-message.md`.

### 5b. Apply the update (one atomic motion)

**Rebuild-only findings do not block the apply**; they become leading caveats
of the results message (a global-dependency bump coupled to a user-created
dependent: name it, check it, offer rollback or a workspace recreate; a
container build/launch parameter a running container cannot adopt: say it
stays inert until a recreate). A genuinely breaking case takes the migration
path below instead.

**When the update touches `system/apps/system_interface/` at all**, also take
the `editing service system_interface` lease through the apply, as
`update-system-interface` does: check `tk ready` for a foreign one (surface
instead of proceeding), then `tk create "editing service system_interface" -t
chore` and `tk start` it, each as its own command. Release it afterwards.

Run the apply from the staged copy, in the **foreground**: its output (refusal
and resume messages, any provisioner warning, the `apply phase timings:` line)
is what you read before recording a verdict.

```bash
python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py apply \
    --merge-ref mngr/update-self --ff-only --target-ref "$REF"
```

When the report names the worker's **built system-interface bundle**, append
`--worker-bundle <that path>` so the exact build the worker validated is
installed instead of a live build.

That one command is the whole landing: it fast-forwards the worker's
`update-self:` merge commit, snapshots the pre-apply state, refreshes the
affected environments, re-runs `system/scripts/setup_system.sh` when a file it
reads changed, pre-flights the merged backend, installs or builds the frontend
bundle, restarts the services agent (every apply), probes the live UI, refreshes
every open view, writes the `docs/VERSION_HISTORY.md` entry, and runs `uv run
env-converge upgrade` -- reverting the entire merge and restoring the
snapshots on any failure. Exit codes:

- **`0` -- applied.** Read the closing stderr lines: a UI that was already
  broken beforehand still exits 0 naming the breakage (report it separately);
  `applied with incomplete provisioning` means one tool-install step is still
  pending and the record at `data/.state/update-apply/provision-incomplete.json`
  is yours to close.
- **`2` -- automatically rolled back.** The entire merge was reverted and the
  workspace confirmed healthy on the previous revision; the update did not
  land. Record `run-status verdict REFUSED --detail "<what failed, one plain
  line>"`.
- **`3` -- emergency.** Even the rollback could not restore health; escalate,
  with the kept pre-apply copies under `data/.state/update-apply/snapshots/`.
- **`1` -- precondition; nothing changed** (dirty tree, `HEAD` moved under the
  pass, another apply in flight, or this merge already landed and rolled
  back). Re-dispatch a fresh worker pass off the current `HEAD`.

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
  `agent_types.claude.version` in `.mngr/settings.toml` in sync). Any
  image-level hunk needs a manual workspace rebuild -- say so.
- **Rebuild-only flags** -- surface as needing a workspace recreate; never
  imply they are live.

Then compose the results message per `references/results-message.md`.

## Migration-required updates

An update that cannot be applied in place (the release restructures something
this workspace's live state was built on with no in-place migration, or the
worker's `stuck` report shows the merge cannot land without breaking the
running workspace) is a terminal verdict, not a confirmation to wait on: tell
the user to create a new workspace and `/migrate-workspace` into it, per
`references/apply-outcomes.md`, and record `run-status verdict
NEEDS_RECREATION --detail "<why, one plain line>"` (with
`--in-place-compatible-ref <X>` when a newer in-place-compatible release also
exists, offered in the same breath).

## 6. Teardown

If a stray system-interface preview is registered (an older pass may have left
one; `update-system-interface` refuses its next pass while one is):

```bash
python3 .agents/skills/update-system-interface/scripts/reveal_system_interface.py unpreview --slug update-self
python3 system/scripts/layout.py close si-preview
```

**The rest is only for a successful apply (exit 0).** After a rollback the
worker's branch, worktree and report are the retry path: keep them until the
retry is resolved with the user, but release the leases either way.

Record the success verdict -- `UPDATED`, or `UPDATED_WITH_REBUILD_ITEMS` when
§5c left something the user must actually do or decide. A purely-deferred item
with nothing observable to act on (a rebuild-only flag that changes nothing
until the workspace is someday recreated) is a results-message caveat under a
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
close` the `editing service system_interface` lease if 5b took one, then the
`updating workspace` lease (`tk close "$UPDATE_LEASE_ID" "Update pass
finished."`), then `tk close <ticket-id> "Updated to <ref> -- worker branch
merged and applied."`.

## To push local improvements back upstream

Use the `submit-upstream-changes` skill -- the complementary direction. This
skill only pulls.

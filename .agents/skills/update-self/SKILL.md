---
name: update-self
description: Safely pull updates from the upstream template repo. Use to incorporate upstream skills, script fixes, or config improvements. For pushing local improvements back upstream, use the `submit-upstream-changes` skill instead.
metadata:
  author: imbue
  exempt-checks: contents
  exempt-reason: awaiting-restructure
---

# Pulling updates from the upstream template, safely

This workspace stays connected to the template it was made from by the
`upstream` git remote (`system/config/parent.toml`). An update never changes
the live tree from an unverified state: a **worker** merges and validates the
release on its own branch, and one atomic, rollback-on-failure **apply**
lands the result. You are the **lead**. A **pass** is one run of this skill,
from Step 1 to teardown.

## Standing instructions

- **The pass is unattended.** Never stop to ask for a go-ahead. Only two
  questions reach the user mid-pass: an `--override` past the version ceiling
  (Step 2), and room for a gate once the licence is spent (Step 4). An update
  that cannot keep something they built ends the pass with a decision.
- **The target is the release the Imbue Studio app driving this workspace was
  built against**, and only that one. Never name a release above `ceiling`
  that the user did not ask for by name, and never resolve a ref by hand.
- **Two copies of this skill run a pass.** Steps 1-2 run from the workspace's
  own. From Step 3 on, everything runs from the copy Step 2a stages at
  `data/.tasks/update-self/skill-at-target/.agents/skills/update-self`: read
  each `references/` file named below there, and run that copy's
  `scripts/update_self.py`, by its full path every time (each bash call is a
  fresh shell).
- **Every pass ends with exactly one `run-status verdict`**; one that ends
  without it shows the user "update failed". `references/apply-outcomes.md`
  ("Which verdict ends the pass") lists them.
- **The licence.** The pass may clear the way for the update without asking
  (stop chats, agents, the browser and apps; prune caches and dead worktrees;
  commit a dirty tree), within `references/freeing-memory-for-an-update.md`.
  Name each such action in the results message, with its undo.
- **Wait for the worker only through the poll**, then end your turn: no
  `sleep`, no reading its reports directory or its pane.
- **Never destroy the worker or delete `mngr/update-self` to start over.** A
  moved `HEAD`, a rollback and a decision all go back to the same worker.
- **Each `tk` call is its own command**, never chained.
- **After a compaction**, re-read this file, then the reference your current
  step names.

## When this is not a fresh pass

Read the one that applies instead of starting at Step 1:

- The user answers a decision a pass ended on:
  `references/resuming-after-a-decision.md`.
- The user overrides a `stuck` verdict: `references/resuming-after-stuck.md`.
- The Imbue Studio app says this chat stopped and was started again:
  `references/resuming-after-a-restart.md`.
- The user says to stop or cancel: `references/stopping-when-asked.md`.
- The user wants an update, or a piece of one, undone:
  `references/apply-outcomes.md` ("Rolling back on request").

## Steps

- [ ] 1. Preconditions: backup check, lease, run record, uncommitted work.
- [ ] 2. Resolve the target; (2a) stage its copy of this skill and hand off.
- [ ] 3. Dispatch the worker: (3a) re-check the ceiling, bridge, stage shared
  prose; (3b) launch and arm the poll. **Ends the turn.**
- [ ] 4. Proxy the `question` gate, when the poll brings one. An answer
  **ends the turn**; a hold ends the pass.
- [ ] 5. Terminal status. On `done`: (5a) audit, (5b) apply, (5c) fix what
  broke, (5d) escalate built-in defects.
- [ ] 6. Teardown, the verdict, show this chat. **Ends the turn.**

## 1. Preconditions

**Check the backup**, the last resort if the apply's rollback and `recover`
both fail. This only reads the service's log:

```bash
uv run --no-sync host-backup-now --check
```

Exit 0 is a recent restore point. 3 (not configured), 4 (out of backup
storage, so the last restore point is older than expected), 1 (no recent
success) and 2 (not observable) mean none is confirmed. **None blocks the
pass**: git still holds every version of the tree. Carry which it was into
the results message as a caveat.

**Take the `updating workspace` lease.** One pass at a time. Look for
another:

```bash
tk ready > /tmp/update-self-inflight.txt
grep "updating workspace" /tmp/update-self-inflight.txt
```

A match is decided by rule, never by asking. `tk show <id>` names its holder
(`Held by <name>`):

- **Your own name**: an earlier attempt of this chat's. `tk close <id>
  "Superseded: its holder is gone."`.
- **A holder `mngr list --format "{name}	{state}"` shows `RUNNING` or
  `WAITING`**: another pass is live. Tell the user an update is already under
  way and end, recording nothing.
- **Any other holder** is gone: take its pass over, or supersede it once its
  worker is gone too, per `.agents/shared/references/harden-contention.md`.

Then take it, and `tk start "$UPDATE_LEASE_ID"`:

```bash
UPDATE_LEASE_ID=$(tk create "updating workspace" -t chore \
    -d "Held by $MNGR_AGENT_NAME across the update-self pass; released at teardown.")
```

**Record the pass for the Imbue Studio app**, only once the lease is yours
(it polls `data/.state/update-apply/run.json`, one record per workspace):

```bash
python3 .agents/skills/update-self/scripts/update_self.py run-status start
```

**Save uncommitted work.** The worker branches off `HEAD`. If `git status
--porcelain` is non-empty, commit everything as one commit on the
workspace's branch, and note the sha for the results message:

```bash
git add -A
{ echo "Save work in progress before the workspace update"; echo
  echo "Uncommitted changes, saved as they stood so the update could run:"
  git diff --cached --name-only | sed 's/^/- /'; } > /tmp/update-self-wip-message.txt
git commit -F /tmp/update-self-wip-message.txt && git rev-parse HEAD
```

If a hook refuses the commit, take the file it names out (`git reset --
<path>`), commit the rest, and name that file in the results message.

## 2. Resolve the target

When the message that started this pass pins the update, set `PINNED_CEILING`
to its `Ceiling:`, and `PINNED_OVERRIDE` to its `Target:` when it says the
user chose that version. For an override the user gives in chat, set
`PINNED_OVERRIDE` to it. Otherwise leave both empty.

```bash
PINNED_CEILING=""
PINNED_OVERRIDE=""
git remote get-url upstream 2>/dev/null || git remote add upstream "$(python3 -c "
import tomllib
with open('system/config/parent.toml', 'rb') as f:
    print(tomllib.load(f)['url'])
")"
# Complete a shallow clone's history (references/older-workspaces.md).
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

Tell the user which version you are updating to.
`references/version-ceiling.md` handles the other two outcomes:

- **A non-zero exit: stop.** "When no target resolves" names the verdict for
  its `error:` line: `ALREADY_CURRENT`, a fault (`STUCK`), or `REFUSED`.
- **`"exceeds_ceiling": true`**: unless the starting message already carries
  the user's confirmation, get their go-ahead ("Overrides past the ceiling").
  Record the wait while you ask, and clear it when they answer with the same
  script's `run-status resume`:

```bash
python3 .agents/skills/update-self/scripts/update_self.py run-status hold \
    --detail "<one plain line: that you are waiting for them to confirm that version>"
```

### 2a. Hand off to the target's own update-self flow

Stage the skill as it is at `$REF`:

```bash
python3 .agents/skills/update-self/scripts/update_self.py bootstrap-skill --ref "$REF" \
    > /tmp/update-self-bootstrap.json || exit 1
DIFFERS=$(python3 -c 'import json; print(json.load(open("/tmp/update-self-bootstrap.json"))["differs"])')
echo "differs=$DIFFERS"
```

A non-zero exit means `$REF` names no commit here: stop, as for a Step 2
error. Otherwise the worker runs from the staged copy either way, and
`differs` decides only which prose *you* follow next:

- **`False`**: continue with this document.
- **`True`**: stop following this document and follow the staged copy's
  `SKILL.md` from **Step 3** onward
  (`data/.tasks/update-self/skill-at-target/.agents/skills/update-self/SKILL.md`).
  Do not re-run its Step 2 or re-stage; carry `$REF` into its Step 3.

Before editing this skill, read `references/handoff-contract.md`: this
boundary and the staging path must stay.

## 3. Dispatch the worker

If another copy of this skill sent you here, and it had no "Standing
instructions" section, read that section above and
`references/older-workspaces.md` before 3a: that file says what differs on a
workspace older than this document.

### 3a. Re-check the ceiling from the staged copy (first, before anything else)

```bash
PINNED_CEILING=""
python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py \
    resolve-target --local-tags --override "$REF" \
    ${PINNED_CEILING:+--app-version "$PINNED_CEILING"} > /tmp/update-self-recheck.json || exit $?
cat /tmp/update-self-recheck.json
```

Set `PINNED_CEILING` as Step 2 does. **A non-zero exit: stop** and follow
what it printed (an `error:` line is a Step 2 error; exit 75 prints its own
procedure). If `exceeds_ceiling` is `true`, follow
`references/version-ceiling.md` ("When Step 3a finds the target above the
ceiling") before going on.

**Then bridge the workspace to the target's history**, fetching the tags
again first, forced:

```bash
git fetch upstream --tags --force
python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py \
    bridge-history --ref "$REF" || exit 1
```

**Then stage the target's shared prose** (again whenever Step 2a is re-run,
which clears the staging directory):

```bash
python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py \
    stage-shared-prose --ref "$REF" || exit 1
```

If either command exits non-zero: `run-status verdict STUCK --detail "<the
error line, in plain terms>"`, surface it, and stop. From the bridge on, a
pass that ends for any reason runs Step 6 first.

From here on, files outside this skill are named by their staged path under
`data/.tasks/update-self/skill-at-target/`. When a staged file names a
further `.agents/...` path, read the staged copy if there is one, else the
workspace's.

### 3b. Launch

Follow `references/launching-the-worker.md` top to bottom, through the
launch, `run-status delegate` and the background poll. The worker follows
`references/update-self-worker.md`. Once the poll is armed, **end your
turn**; its message wakes you with the worker's report.

## 4. Proxy the `question` gate

Read `references/question-gates.md` when the report's `name:` is `question`.
It is a merge conflict to decide, a scope question to answer by the rule it
names, a request for memory, or a **hold**: a customization the update cannot
keep, which ends the pass `NEEDS_DECISION` with the worker stopped and kept.
For the first three: reply, re-arm the poll, end your turn. Answering a
worker in general is
`data/.tasks/update-self/skill-at-target/.agents/shared/references/lead-proxy.md`.

## 5. Terminal status

- **The worker died without reporting**: recover it per
  `data/.tasks/update-self/skill-at-target/.agents/skills/launch-task/references/dead-worker-recovery.md`,
  then re-arm the poll. It is stuck only once that has not brought it back.
- **A `stuck` showing the merge cannot land without breaking the running
  workspace**: `references/apply-outcomes.md` ("Migration-required
  updates"); the pass ends `NEEDS_RECREATION`.
- **Any other `stuck`**, or a dead worker that did not come back: nothing is
  merged or applied. Surface it per `references/results-message.md` ("The
  `stuck` message"), record `run-status verdict STUCK --detail "<one plain
  line on what failed>"`, and go to Step 6.
- **`done`**: 5a to 5d, in order.

### 5a. Audit the report

Check the report against `references/auditing-the-report.md`. The audit, not
the user, authorizes the apply, and a report that lacks evidence goes back to
the worker.

### 5b. Apply the update (one atomic motion)

Follow `references/applying-the-update.md` top to bottom: catch the worker up
if `HEAD` moved, pause the other chats, save their work, take the
critical-app leases, run `update_self.py apply` in the foreground, bring the
chats back. For any exit code but 0, and for the closing lines of an exit 0,
read `references/apply-outcomes.md` before doing anything else.

### 5c. Carry out the report-driven remainder

After a landed apply, anything of the user's that is broken gets fixed, not
reported: `references/after-the-apply.md`, which also says what each finding
of the report asks of you. The worker classed provisioning findings by
`references/worker-provisioning-changes.md`; read it when a class is unclear.

### 5d. Escalate the built-in defects this pass found

Escalate them together, per `references/after-the-apply.md` ("Escalating
built-in defects"). Then compose the results message per
`references/results-message.md`.

## 6. Teardown

Follow `references/teardown.md`, whatever the outcome. It ends by showing the
user this chat: end your turn there with the results message, the decision,
or the `stuck` account.

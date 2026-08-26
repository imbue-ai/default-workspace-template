---
name: update-self
description: Safely pull updates from the upstream template repo (default target is the latest stable release the running Minds app supports). Use when you want to incorporate upstream skills, script fixes, or config improvements. For pushing local improvements back upstream, use the `submit-upstream-changes` skill instead.
metadata:
  author: imbue
---

# Pulling updates from the upstream template, safely

This repo was created from a template repo and stays connected to it via a git
remote (`system/config/parent.toml` has the URL and branch). Upstream carries the shared
infrastructure: skills, scripts, `CLAUDE.md` scaffolding, `Dockerfile`,
`system/supervisord.conf`, the system interface, the vendored `mngr`.

Merging upstream can break the live workspace -- a settings-schema change the
running `system_interface` can't parse, a bumped `system/vendor/mngr`, a new service.
So, like `update-system-interface`, this flow never mutates the live tree from an
unverified state: an isolated **worker** does the merge and validation on its own
branch, and only a known-good, validated result is landed and applied.

You are the **lead**, and the pass is **fully unattended**: once the user
starts it, run it end to end -- resolve the target, dispatch the worker,
answer its gates, audit its evidence, run the one-command **apply** that
lands the merge and makes the live workspace consistent with it (Step 5b),
and only then report. The user launches an update and walks away; they come
back to a finished result and an offer to roll back anything they don't
like, not to questions. This is safe because everything the apply lands is
git (usually with a host backup behind it) and the worker validates before
anything goes live -- so review happens *after* the apply instead of gating
it. The one thing that still waits for the user is an update that cannot
keep something they built (the Step 4 hold) -- the question they would
genuinely want asked. The worker owns the merge, the conflict triage, and
the validation; the apply script owns going live.

The default target is the **latest stable `minds-v*` tag** (released,
already-tested), not `origin/main` -- and never newer than the Minds app driving
this workspace, since the template ships the code that app talks to. The user may
override to a specific tag or to `main`.

Because the update flow itself evolves, once the target is resolved this pass
**re-points itself at the target version's own copy of the update-self skill**
(Step 2a) and runs the rest -- lead *and* worker -- from there. So a fix to the
conflict triage, validation, or apply logic that shipped in the release is
applied on the way *in*, instead of staying a release behind in the local copy.
That copy is staged at one fixed path --
`data/.tasks/update-self/skill-at-target/.agents/skills/update-self` -- which the lead
and worker both address by literal (no shell state carried between commands, since
each bash invocation starts a fresh shell).

## 1. Preconditions

**Back up first.** Before dispatching anything, capture a restore point of the
whole workspace so the pass is recoverable -- the apply re-runs provisioners and
restarts services, and a backup is the last-resort recovery path if everything
else (the apply's own rollback, `recover`) fails:

```bash
uv run host-backup-now
```

It waits for any in-flight backup, forces a fresh tick, and prints the tick's
terminal event -- exit 0 means `restic_backup_succeeded`. Exit 3 means backups
aren't configured (`tick_skipped_due_to_missing_secrets` -- no
`data/.secrets/restic.env`), so there is **no** restore point. Exit 1 is a
failed backup attempt; exit 2 means the outcome could not be observed at all
(the tick may still be running, or the service is not writing events) --
neither confirms a restore point. **None of the three blocks the pass**: note
which it was and carry it into the results message (§5a composition rules) as
a caveat, then continue -- git still holds every version of the tree, and the
apply's own rollback and `recover` are the primary recovery path regardless.
Do not stop to ask for a go-ahead.

**Take the "updating workspace" lease.** One update flow at a time (its worker
name, branch, and runtime dir are fixed, and two applies must never
interleave). This is a lease like the other flows' editing leases, held from
here through the worker, the report audit, and the apply, and released in
Step 6. First check for a foreign one:

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

**Clean tree.** The worker branches off your `HEAD` and the rollback captures it.
If `git status --porcelain` is non-empty, surface it and stop.

## 2. Resolve the target

Ensure the remote exists, fetch with tags, and resolve the ref:

```bash
git remote get-url upstream 2>/dev/null || git remote add upstream "$(python3 -c "
import tomllib
with open('system/config/parent.toml', 'rb') as f:
    print(tomllib.load(f)['url'])
")"
# Heal a shallow-history workspace before fetching. Workspaces created from a
# pool host baked before the full-history bake fix carry a `--depth 1` clone:
# `git log` dead-ends at a parentless graft commit, `git describe` fails, and
# "what changed since the fork point" is unanswerable. The upstream template is
# public, so completing the history is one fetch; the guard keeps this a no-op
# everywhere else (`--unshallow` errors on a repo that is not shallow).
# `--git-common-dir` (not `--git-dir`) because the shallow marker is repo-wide
# state that lives in the common dir, which is what a worktree checkout shares.
if [ -f "$(git rev-parse --git-common-dir)/shallow" ]; then
    git fetch --unshallow upstream
fi
git fetch upstream --tags

python3 .agents/skills/update-self/scripts/update_self.py resolve-target --local-tags \
    > /tmp/update-self-target.json || exit 1
# `--local-tags` reads the tags the fetch above just landed (no second network
# round-trip). Honoring a user override, append e.g. `--override main` or
# `--override minds-v0.3.6` to the resolve-target call above. The `|| exit 1`
# leaves a refusal's `error:` line as the last thing printed -- without it the
# read below fails on the empty file and buries that line under a traceback.
cat /tmp/update-self-target.json
REF=$(python3 -c 'import json; print(json.load(open("/tmp/update-self-target.json"))["ref"])')
```

`resolve-target` prints `{"ref": ..., "kind": "tag|branch|ref", "ceiling": ...,
"exceeds_ceiling": ..., "latest_available": ..., "held_back_by_ceiling": ...}`;
`main` resolves to `upstream/main` (not the stale local branch). Keep `$REF` in
your shell for the dispatch below, and tell the user which version you're
updating to.

**If the command exits non-zero, stop -- nothing is wrong with the workspace.**
It prints a single plain-language `error:` line saying why no target could be
chosen. Relay *that* line in plain terms per the §5a composition rules and offer
the next step; the usual reasons each have a different answer for the user: the
minds app could not be reached (it is closed, or the gateway is down -- retry once
it is running); the app is too old to report its version (update the minds app
itself first, then re-run); every release upstream is already newer than the app;
or the workspace is already on the release it may take -- which is either "you are
current" or "updating the app unlocks the newer one," and the `error:` line says
which. Do **not** work around it by resolving a ref by hand.

### The version ceiling

The default target is capped at the version of the **minds app driving this
workspace**, which `resolve-target` reads from the app itself (`ceiling` in the
output). This matters because the template carries the code the app talks to --
the system interface and the vendored `mngr` -- so a workspace running a template
newer than its app would be speaking a protocol the app does not know. When the
app reports a branch rather than a release tag (a dev build) there is nothing to
compare against and `ceiling` does not cap anything.

A workspace already sitting *at* the ceiling gets a refusal rather than a pass:
the capped target is the release it was created from, so there is nothing to
merge, and `resolve-target` says so instead of spending a backup, a worker and a
validation run on a no-op -- naming the newer release the app is holding back
when there is one. A workspace *behind* the ceiling still updates to it: being
capped is not the same as having nothing to gain, which is why the two cases are
distinguished by whether the resolved ref is already an ancestor of `HEAD` and
not by the ceiling alone.

**`"exceeds_ceiling": true` means the user's `--override` names a version this
app cannot vouch for** -- newer than the app, or a branch/commit whose version
can't be compared. Do not dispatch the worker on it silently. Tell the user
plainly what they asked for and what it risks ("that version is newer than your
Minds app, so parts of your workspace may stop working until you update the app
itself"), and **get an explicit go-ahead before continuing**. This is the one
confirmation the otherwise-unattended flow keeps: it fires immediately at
launch, while the user is still present, and it asks whether to *attempt* an
unsupported version at all -- a question no later rollback offer can substitute
for. An override at or below the ceiling needs no confirmation.

To preview what the release actually changes, always diff from the **merge
base**, never from `HEAD` -- a `git diff HEAD "$REF"` also shows every *local*
change as if upstream were reverting it, which reads as phantom upstream churn:

```bash
git diff --name-status "$(git merge-base HEAD "$REF")" "$REF"
```

### 2a. Hand off to the target's own update-self flow

Now re-point the rest of this pass at the update-self skill **as it exists at
`$REF`**. Stage that copy (from the already-fetched objects -- no network, no
working-tree mutation) at the fixed path
`data/.tasks/update-self/skill-at-target/.agents/skills/update-self`, and learn
whether it differs from your local one:

```bash
DIFFERS=$(python3 .agents/skills/update-self/scripts/update_self.py bootstrap-skill --ref "$REF" \
    | python3 -c 'import sys, json; print(json.load(sys.stdin)["differs"])')
echo "differs=$DIFFERS"
```

`bootstrap-skill` always leaves a runnable flow at that fixed path (the target's
copy, or -- when the ref predates the skill -- the local copy), so **the worker
runs from `data/.tasks/update-self/skill-at-target/.agents/skills/update-self`
regardless**. `differs` decides only which `SKILL.md` prose *you* follow next:

- **`differs` is `False`** (the staged flow is byte-identical to yours, or the ref
  predates the skill) -> **continue with this document**.

- **`differs` is `True`** -> the target's copy of the flow differs from your local
  one (it shipped changes, or this workspace customized the flow locally). **Stop
  following this document** and follow the staged copy's `SKILL.md` from **Step 3**
  onward:

  ```bash
  # read and follow "data/.tasks/update-self/skill-at-target/.agents/skills/update-self/SKILL.md" from Step 3
  ```

  You have already completed Steps 1-2 (backup, the updating-workspace lease,
  clean tree, target resolved), so do **not** re-run the staged doc's Step 2 or
  re-stage -- just carry
  `$REF` forward into its Step 3.

Either way, `data/.tasks/update-self/skill-at-target/.agents/skills/update-self` now
holds the copy of the flow to run. Everything below reaches the skill's scripts
and worker reference through that literal path (and points the worker at it), so
both dispatch against the correct version.

**The handoff contract (keep this boundary stable when editing this skill).**
Steps 1-2 -- preconditions and target resolution -- always run from the *local*
copy: they are what decide `$REF`, so by construction they cannot come from the
target. The target's flow is entered at **Step 3**, and everything from there
on -- the worker dispatch, the report audit, **and the apply** (Step 5b runs
the staged copy's `update_self.py apply`) -- is the target version's. So an
edit to this skill must preserve that boundary: a future version's Steps 1-2
must stay "capture a backup, the lease/clean-tree checks, then resolve a ref
into `$REF`", and its Step 3 must stay the worker dispatch -- otherwise an
older initiator handing off into a newer copy (or vice versa) lands at the
wrong step. Because the apply runs from the staged skill-at-target copy, an
old workspace updating in runs the *target's* apply -- fixes to the apply flow
take effect for the very update that ships them, which also means the apply
must keep tolerating older pre-merge trees (guarded imports, no assumptions
about pre-merge layout). The version ceiling is
part of resolving `$REF`, so Step 2 computes it from the *local* copy -- which on
a workspace whose template predates the ceiling does not compute one at all.
Step 3a therefore re-checks it from the staged target copy before the dispatch,
so the cap holds on the very first update into it. Keep 3a in any future
version: it, not Step 2, is what protects a workspace arriving from an older
template. Keep the staging path
(`data/.tasks/update-self/skill-at-target/.agents/skills/update-self`) stable for the
same reason. Note also that this handoff runs the target ref's `update_self.py`
and follows its prose *before* the worker has validated anything; for the default target
(a stable, already-tested `minds-v*` tag) that is the same trust basis as the
merge itself, but a `--override` to an untrusted ref means trusting that ref's
flow code and instructions -- only override to a ref you trust.

## 3. Dispatch the worker

### 3a. Re-check the ceiling from the staged copy (first, before anything else)

Run the version ceiling once more, from the **staged target copy**:

```bash
python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py \
    resolve-target --local-tags --override "$REF" > /tmp/update-self-recheck.json || exit 1
cat /tmp/update-self-recheck.json
```

**This is not redundant with Step 2 -- it is the only ceiling check that runs on
a workspace updating *into* the ceiling for the first time.** Step 2 runs from
this workspace's *local* skill copy, and any workspace whose template predates
the ceiling has a local copy that does not check one: it happily resolves the
newest tag upstream, which is exactly the too-new target the ceiling exists to
refuse. The staged copy is by construction at least as new as `$REF`, so this
check runs no matter how stale the initiator was. That is precisely what §2a's
hand-off machinery is for -- "a fix that shipped in the release is applied on the
way *in*" -- and the ceiling is such a fix.

If `exceeds_ceiling` is `true` here and the user has **not** already confirmed an
over-ceiling `--override` in Step 2, stop and take that confirmation now, with
the same plain-language framing Step 2 describes. A default (no-override) resolve
that trips this means the local copy chose a target its app cannot support:
say so, and offer the ref this pass *would* cap to (re-run without `--override`
to learn it). Do not dispatch the worker until it is resolved.

**If the user takes the capped ref instead**, set `$REF` to it and then re-run
§2a for the new `$REF` before dispatching. Do not just reassign the variable:
§2a staged the skill at the *old* `$REF`, and the staged copy is what supplies
the worker guide, the `update_self.py` both of you run, and the prose you are
reading -- leaving it in place would run the too-new release's flow against a
target that is not it. `bootstrap-skill` re-stages destructively, so re-running
it is safe, and 2a's `differs` branch then decides which document you follow, as
on the first pass. The capped ref is by construction at or below the ceiling, so
the second time through 3a clears.

The boundary in §2a still holds: Step 2 is what *resolves* a target and Step 3 is
the worker dispatch. 3a resolves nothing -- it either clears the target Step 2
chose or hands the pass back to 2a with the ceiling's answer.

### 3b. Launch

First, surface your own chat tab. The minds app sends the user into this
workspace when it starts an update, and this conversation is what they are
meant to land on; the workspace UI places the tab in front of whichever client
is connected when your chat appears, which from outside the workspace is often
nobody yet. The command detaches a helper that keeps trying until a client is
there (or gives up after a while), so it returns at once; it is best-effort,
and a failure is not a reason to stop:

```bash
python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py \
    surface-chat-tab --name "$MNGR_AGENT_NAME"
```

Then open a tracking ticket, write the task file, launch via the `launch-task`
machinery, and background-poll.

```bash
mkdir -p data/.tasks/update-self
tk create "update-self" -t task \
    --acceptance "worker launched; conflicts triaged; validated; branch applied"
```

Note the ticket id it prints, then start it. The tk hook requires `tk start` /
`tk close` to be the *only* command in their tool call -- never chain them after
another command or capture their output:

```bash
tk start <ticket-id>
```

Write the task file. Use the two-heredoc form the other worker skills use: an
**unquoted** frontmatter block so `$MNGR_AGENT_NAME` and `$REF` expand, then a
**quoted** body so its backticks stay literal.

Unlike every other worker skill, this template DOES set `lead_agent`, and the
line must stay: this SKILL.md is executed cross-version -- an older workspace's
lead follows this staged prose (via the `differs` branch of §2a) but launches
with its *own* `launch-task/create_worker.py`, which may predate launch-time
`lead_agent` stamping. Under a current launcher the line is harmless (launch
overwrites it from the environment); under an old launcher it is the only thing
that gives the worker a report address.

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

Launch with the plain `worker` template (this flow uses its own worker guidance,
not the generic `harden-worker`), then background-poll (`run_in_background:
true`), re-arming per `lead-proxy.md`. `--destroy-existing` clears the previous
pass's worker, which Step 6 leaves *stopped* rather than destroyed (so its
transcript stays reachable for bug reports); a previous worker that is still
running is a genuine conflict and the launch refuses it -- resolve that per the
lease check in Step 1 rather than forcing past it:

```bash
uv run .agents/skills/launch-task/scripts/create_worker.py launch \
    --name update-self --template worker --destroy-existing \
    --runtime-dir data/.tasks/update-self/ --task-file data/.tasks/update-self/task.md

uv run .agents/skills/launch-task/scripts/create_worker.py await \
    --name update-self --task-file data/.tasks/update-self/task.md --timeout 90m
```

## 4. Proxy the `question` gate

Per `.agents/shared/references/lead-proxy.md` (worker `update-self`, branch
`mngr/update-self`, reports dir `data/.tasks/update-self/reports/`). A
`question` is one of three things; the first two you answer yourself, and only
the third reaches the user.

For a genuine, unresolvable **merge conflict** -- a real decision about how to
reconcile a file both sides rewrote incompatibly -- **decide it yourself**:
the pass is unattended, the average user has no opinion on a technical
conflict, and a wrong call is recoverable because the results message names
the decision and keeps the other side on offer. Relay your resolution via
`mngr message`, consume the report, and re-arm.

The second kind of `question` is the worker's review-gate escape hatch
(its §4c): a *process* question about whether or at what scope the gates run.
That is not the user's to answer -- **answer it yourself** per `lead-proxy.md`.
The default answer is to apply the §4c rule as written: the rule already is the
proportionality decision, so any situation it covers gets the branch the rule
gives it. Where the rule is genuinely silent -- the case §4c routes here --
decide it on the rule's own principle rather than on the worker's proposal: the
fallback is always more coverage, never less, so the gates run unless the worker
has shown the skip branch's three conditions hold. Either way, reply, consume,
and re-arm without involving the user. Escalate only if the worker has surfaced
a real question of user intent inside it.

For the conflict case, the default that needs no deliberation is to **keep
this workspace's current behavior**: preserve the user's local customization
and fold in what upstream adds around it, taking the release's side only
where no local intent is at stake (formatting, generated files, code the user
never touched). Record every such decision -- the file, what was kept, what
the release's version would have changed -- because the results message must
present each one with the alternative still on offer ("I kept your version;
if you'd rather match the official release exactly there, I can do that").
When a conflict genuinely has no safe default, still prefer the local side
and make that decision a leading caveat of the results message rather than
stopping the pass -- unless it rises to the customization hold below, which
is judged on the outcome for the user's own creations, not on the merge
mechanics.

The third kind of `question` is the worker's **customization hold** (its §4b
survival verdict): something the user built -- a widget, an app hooking into
the system interface's API or state, a customized surface -- that the update
**cannot keep**, even after the worker genuinely tried to re-fit it on the
new base. This is the one gate that escalates to the user, because it is the
one question they would actually care about: apply-then-offer-rollback is a
bad remedy when either choice visibly breaks their thing right now. Compose
it from their point of view, per the §5a composition rules: what they built,
what the update does to it, the worker's before/after evidence, and the
concrete choices -- apply the update and lose it, skip the update, or the
worker's best adaptation option -- with a recommendation. Reassure that
nothing has been applied and the workspace is untouched, and **wait**: if the
user is away, the pass holds (worker alive, leases held) until they answer.
A cosmetic shift -- the widget moved but still works -- is *not* a hold: that
applies unattended and is named in the results message with an offer to
restore the old arrangement.

## 5. Terminal status

- **`stuck`** or a dead-worker timeout -> surface via
  `.agents/skills/launch-task/references/worker-failure.md`. Nothing is merged or
  applied; the live workspace is untouched. **Don't relay the raw failure, but
  don't strip the specifics either** -- this is the one message type where
  technical detail is *preserved, not dropped*, because the user often forwards a
  failure into a bug report and it must stand on its own to whoever reads it next.
  Compose it in two parts: a **plain-language lead for the user** (what happened
  and what it means -- "I couldn't complete this update cleanly; your workspace is
  untouched and nothing was applied" -- plus a next step or an offer to work
  through it together), followed by a **clearly-marked technical detail block for
  whoever they escalate to**: the target ref, the step or phase that failed, the
  specific file or component, and the **actual error text or log excerpt
  verbatim** (not paraphrased), with a pointer to the full report and logs under
  `data/.tasks/update-self/reports/`. Never leave the user at a dead end, and never
  hand them a failure so vague it's useless in a bug report.
- **`done`** -> the report audit below.

### 5a. Audit the report; compose the results message after the apply

**Audit the report before composing anything.** The worker contract (the
staged copy's `references/update-self-worker.md`, §4c and §6) makes the review
gates rule-driven and the report evidence-bearing: it must either show the
clean-pull skip's conditions held (`has_merge_work: false`, no impacted
user-created code, and no worker-authored in-branch edits such as 4a mirror
edits) or carry the gate run's own evidence (fix commits
kept/reverted, or a clean gate run, plus architecture-gate verdicts). Likewise
a side-picked conflict must carry the discarded-side accounting, not a bare
"superset" claim. A report missing any of this -- including one that openly
discloses skipping or narrowing a gate outside the rule -- goes back to the
worker to be completed: run the Step 4 gate cycle over it (say what is missing
via `mngr message`, consume this report into
`data/.tasks/update-self/reports/consumed/`, re-arm the background poll) and
audit the replacement, because `done` otherwise ends the poll and a report left
at the report path would satisfy the next `await` instantly. Do not run the
apply over the gap, and never repackage a worker-disclosed deviation as
reassurance. A deviation only *stands* when completing it is genuinely out of
reach -- the worker is gone and the gap cannot be closed from here; not because
the reasoning behind it persuaded you. In that case the results message states
the deviation itself, plainly, where the user will read it -- it is a caveat,
never a footnote to a reassurance.

The `done` report is *your* raw material, not the user's message. It is a
comprehensive, technical digest for the lead -- changelog entries in range, the
conflicts and how the worker resolved them, change-class breakdown, impact
analysis, and validation. **Do not forward it verbatim.** Keep it available (it
is persisted under `data/.tasks/update-self/reports/` -- offer to show it if the
user wants the specifics), and **compose a plain-language results message**
from it -- delivered *after* the apply. There is no approval gate: run 5b as
soon as the audit above passes; the audit, not the user, is what authorizes
the apply. The results message is the one thing the user reads about the whole
pass -- what changed, every decision made on their behalf, any caveats, and
the standing offer to roll back anything they don't like.

**These composition rules govern every user-facing message this flow produces --
the results message here, the Step 4 customization hold, and a `stuck` result
(Step 5) alike.** Whenever the update can't simply proceed, the message names the
blocker in plain terms and **proposes a way forward, or invites the user to
resolve it with you** -- it never dead-ends. The one thing that varies is how
much mechanism to keep: the results message drops technical
detail the user can't act on, but a `stuck` message deliberately preserves it
(Step 5) so it survives being pasted into a bug report.

Write the message a non-technical reader skims top-to-bottom, in this fixed
order:

1. **Verdict headline** (one line, first thing they see): "your workspace is
   updated," "updated, with one thing to know," or -- after a rollback -- "the
   update hit a problem, so I undid it; everything is safe."
2. **Held back by your app version** -- include this line if and only if
   `held_back_by_ceiling` is `true` in the resolve-target output
   (`/tmp/update-self-target.json`). Say it in one plain line -- "there's a newer
   version available (`latest_available`), but it needs a newer Minds app than
   you're running, so I stopped at X" -- so the user understands why they aren't
   getting the newest thing and knows updating the app unlocks it. Do **not**
   derive this yourself by comparing `ref` against `latest_available`: those two
   also differ when the *user's own* `--override` picked an older tag, and saying
   "your app held this back" there blames the app for the user's choice. The flag
   already accounts for that.
3. **What's new** -- always first after the ceiling note. Keep this *detailed*:
   some readers want the specifics, others happily skim it as "great, they're on
   it." Do not thin it out -- carry the worker's digest, just in prose a lay reader
   parses (describe what each change does, not the file names).
4. **Conflicts** -- "none," or what needed reconciling. When the worker kept
   local code over the release's version of the same file, do not present that
   as a settled fact: say what was kept, what the release's version would have
   changed, and offer the alternative in the same breath ("I kept your
   version; if you'd rather match the official release exactly there, I can do
   that instead"). The choice between a local divergence and the tested
   release is one the user may well care about, and it is cheap to offer now
   and expensive to unwind later.
5. **Your customizations** -- when the report classed a user creation
   intact-but-changed, show it: what moved or changed (describe the
   before/after; attach the worker's evidence when the surface supports it)
   and the offer to restore the old arrangement. A cannot-be-kept creation
   never reaches this message unresolved -- it already stopped the pass at
   the Step 4 hold.
6. **Validation** -- did the suite pass; is any failure pre-existing/unrelated.
7. **Caveats** -- only if any; what to expect after applying.
8. **Pre-existing issues** -- only if any, and only after verifying attribution
   (see the worker guidance's §4a): state plainly whether each lives in
   **built-in** code (present at the target ref -> report upstream) or the
   **user's own** code. Never call built-in code "workspace-added."
9. **The offer** -- see the language rule below.

**Detail in the informational sections (3-6); plain language at the decision
points.** Spend deliberate plain-language care only where the message asks the
user to **decide or act** -- the verdict headline, any caveat that needs their
action, and the closing offer. Those carry no jargon: never "merge," "land," or
"fast-forward" there. Frame the close around *what changed in their workspace
and how to undo it* (many users just want their workspace improved and don't
think in terms of merges), e.g. "Your workspace is updated -- if anything looks
or behaves differently than you'd like, tell me and I can put it back."

**Drop dependency/lockfile mechanics** from the user message unless there is an
action the *user* must take. **Command rule:** never print a command *you* will
run -- describe it in plain language ("I'll refresh it automatically if it comes
up"). Show a literal command only when the *user* must run it themselves.

**No previews in the unattended pass.** The old flow stood up a live preview
of the merged system interface (and optionally other user apps) before
applying, whenever the worker had done nontrivial merge work there.
Unattended, the tested result simply lands and the live workspace is the
review surface: when the report marks a surface's merge work nontrivial, name
that surface in the results message, say what was reconciled, and attach the
rollback offer to exactly that piece. (The preview machinery still exists in
`update-system-interface` for its own local-edit flow; this pass does not use
it.)

### 5b. Apply the update (one atomic motion)

**Carry rebuild-only findings into the results message; they do not block the
apply.** The apply is deterministic and has no opt-outs: it re-runs the
provisioner whenever a provisioner-classified file changed, and there is no
flag to land the merge without that. When the worker's report flags either of
these, apply anyway and make the finding a *leading caveat* of the results
message -- named plainly, never a footnote:

- **A global-dependency bump coupled to a user-created dependent.** The worker
  classifies this "unsafe to hot-apply" -- upstream never tested their code
  against the new dependency, and the apply *will* move the global tool under
  that code, because the provisioner re-run is not optional. In the results
  message name the dependent, check it yourself (or invite the user to
  exercise it), and give both remedies in the same breath: roll the update
  back, or recreate the workspace -- which provisions the new substrate and
  re-runs their code against it -- if they want the update *and* a clean
  landing for that code.
- **A container build/launch parameter a running container cannot adopt** (a
  `build_arg` / `start_arg` / runtime flag). Here taking the update is safe --
  nothing hot-applies -- but that one piece stays inert until a recreate, and
  the user has to know that rather than assume it is live.

For a genuinely breaking case, take the migration path below instead -- that
is a terminal verdict of the pass, not a confirmation to wait on.

**When the update touches `system/apps/system_interface/` at all** (merged
*or* pulled in), additionally take the `editing service system_interface`
lease and hold it through the apply, exactly as `update-system-interface`
Step 4 does -- the apply's auto-rollback restores a captured revision, so a
foreign system-interface merge landing mid-motion could be swept away by it.
Check `tk ready` for another agent's lease and surface instead of proceeding
if one is held; then `tk create "editing service system_interface" -t chore`
and `tk start` it (each as its own command). Release it after the apply.

Then run the **general apply** -- from the staged skill-at-target copy, so the
target version's apply logic lands its own release:

```bash
python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py apply \
    --merge-ref mngr/update-self --ff-only --target-ref "$REF"
```

When the worker's report names its **built system-interface bundle** (it built
the frontend for validation), append `--worker-bundle <that path>` -- the
apply then installs the exact build the worker validated instead of spending
a live build (which remains the fallback).

That one command is the whole landing: there is no agent-prose pause between
the merge landing and the workspace being consistent with it. It fast-forwards
the worker's `update-self:` merge commit (preserved verbatim -- the marker
`assist` relies on), snapshots the pre-apply state (built bundle, root
`.venv`, both uv tool environments, `node_modules`), refreshes the affected
environments, re-runs `system/scripts/setup_system.sh` when
provisioner-classified paths changed (before any restart), installs or builds
the frontend bundle, pre-flights, restarts the services agent when anything
restart-requiring changed (system-interface backend, vendored-mngr source,
`.mngr/settings.toml`, supervisord/bootstrap), probes the live UI to the
frontend standard, refreshes every open view, writes the
`docs/VERSION_HISTORY.md` ledger entry, and runs `uv run env-converge upgrade`
-- all inside a single near-OOM-exempt process that reverts the entire merge
and restores the snapshots on any failure.

Interpret the exit code and report it per the §5a composition rules:

- **`0` -- applied.** The update is landed, recorded in the version history,
  and the live workspace confirmed healthy; the environment advanced to the
  merged apt snapshot (summarize the env-converge delta count in plain
  language). **Read the closing stderr lines before signing off**: a workspace
  whose frontend was already broken beforehand still lands and still exits
  `0`, but the final line names the breakage instead of confirming health --
  pass that on as a separate problem, never repackage it as success. A
  non-fatal warning (the ledger could not be committed, or env-converge
  failed) also rides on stderr with its own follow-up; carry it out or
  surface it.

  **`applied with incomplete provisioning` is the other exit-`0` variant to
  act on.** A provisioner run (`system/scripts/setup_system.sh`) that fails
  does *not* roll the update back on its own: a failed tool install leaves
  the tree and services consistent, so the apply carries on to the restart
  and the probes, and a load-bearing provisioner change (a node bump, a new
  apt dependency) still fails those and still rolls back. When the probes
  pass over a failed provisioner, the update is landed and the gap is
  recorded at `data/.state/update-apply/provision-incomplete.json` (the
  reason, the merge, the agent). That record is yours to close: diagnose the
  failure from the stderr excerpt (often no network, or a download that
  never completed), fix the cause, and re-run the provisioner by hand --
  `bash system/scripts/setup_system.sh` -- which is idempotent. Only an
  apply's own successful provisioner run clears the record automatically, so
  once your manual run exits 0, remove it yourself
  (`rm data/.state/update-apply/provision-incomplete.json`); do not leave it
  for a later apply. Tell the user plainly that the update is in and working
  but one tool-install step is still pending, never that everything
  completed.

  Every apply that had live work to do (a `0` after changes, a `2`, or a `3`;
  not the "nothing live needed to change" exit) also prints one `apply phase
  timings:` line -- per-phase durations from the apply marker. It is the
  benchmarking input for the apply's poll and step budgets; when an apply took
  unusually long, quote it in the report rather than guessing which step was
  slow.
- **`2` -- automatically rolled back.** The apply reverted the **entire
  landed merge -- every class, not just the failing one** -- and restored the
  pre-apply state; the live workspace is confirmed healthy on the previous
  revision. The requested update did NOT land. See "If the apply rolled back"
  below for what to tell the user. The same already-broken-frontend variant
  applies here. A forward step that outlives its wall-clock budget (`<step>
  did not finish within <N>s`) is one of the causes that lands here: the
  apply never waits open-endedly on a hung `npm ci`, build, refresh,
  provisioner, or restart, so treat that message as "it hung", with the
  `apply phase timings:` line saying where.
- **`3` -- emergency.** Even the rollback could not restore a healthy
  workspace. Escalate immediately. The pre-apply copies are kept under
  `data/.state/update-apply/snapshots/`, and when the apply touched the
  frontend the stderr names the bundle copy -- putting that one back is a plain
  file copy needing neither npm nor a registry, so pass the path on with the
  escalation. Read the stderr rather than assuming a path is there: after a
  backend-only or provisioner-only apply it names none.
- **`1` -- precondition; nothing changed.** A dirty tree, a refused
  fast-forward (`HEAD` moved under the pass -- treat as stale per
  `.agents/shared/references/harden-contention.md` and re-dispatch off the
  current `HEAD`, never hand-resolve), another apply in flight, an
  interrupted apply of a *different* merge that needs `recover` first, or this
  merge having already been landed **and rolled back**. That last one is the
  one to read carefully: the rollback is a forward revert, so the merge stays
  in history while its content does not, and re-running the apply cannot
  re-land it. Re-dispatch a fresh worker pass off the current `HEAD`.

**If the apply is interrupted** (your chat crashes, the process is killed):
nothing is stranded. The apply writes a marker under
`data/.state/update-apply/` before the merge and updates it per phase, and
every step tolerates re-entry -- so when you come back, simply **re-run the
exact same `apply` command**; it resumes from the recorded state. If nobody
comes back, the workspace heals itself: bootstrap rolls a stale marker back at
the next container start, and a recovery cron (`recover --if-stale`) does the
same within minutes for a kill without a restart -- both then leave the tree
matching the pre-update revision, with the worker branch intact. As above, that
is a forward revert rather than a rewind, so re-running the apply of that same
merge is refused; a retry means a fresh worker pass off the current HEAD.

One carve-out, and it applies exactly once per workspace: both unattended
triggers live in the *running* container, not in the merge being applied.
Bootstrap's boot-time check is the code that booted this container, and the
recovery cron is the entry that bootstrap wrote at that boot -- so on the very
first apply that lands this machinery into a workspace, neither exists yet, and
an interruption before it restarts is only recoverable by re-running the apply
by hand. Say so to the user if that is the update you are running; every apply
after it is covered.

**If the rollback itself could not restore a healthy workspace** (exit 3), the
apply leaves an `emergency.json` beside the marker naming the reason, the agent
that was driving the apply, and where the pre-apply copies were kept, and the
system interface shows a banner reading it. That state does not resolve on its
own -- it is the one that wants a person -- so treat the record as the starting
point rather than re-running anything blind.

Nothing takes that record down by itself, and the banner keys off its mere
presence, so **clearing it is part of the repair**. Only an outcome that ends
with the live workspace *confirmed healthy* clears it, and the frontend counts:
a later update that lands over a UI it could confirm, or a `recover` of a *new*
interruption that probes the workspace afterwards. Neither describes the
ordinary case, and two nearby outcomes are deliberately not among them -- the
boot-time recovery runs before anything is up, so it has nothing to probe, and
an apply over a UI that was already broken exits `0` naming that breakage
rather than confirming health. Both leave the record alone. This exit already
cleared the marker and its rollback already put the tree content back, so a
bare `recover` finds nothing to do, and re-running the same `apply` is refused
(the merge is landed-and-rolled-back). So when the repair was by hand: verify
the workspace is actually healthy, then delete
`data/.state/update-apply/emergency.json` and tell the user what happened.
Leaving it in place means a workspace that is fine still telling its user it
may be broken.

### 5c. Carry out the report-driven remainder

The apply covers everything deterministic. What is left is exactly what needs
the worker's impact analysis, so work the report:

- **`shared_runtime` live consumers** -- a changed `system/scripts/**`,
  `system/libs/**`, `system/services/**`, `system/apps/**`, or `.agents/**`
  file applies to future agents automatically, but the report's impact
  analysis names any *live* service depending on it that the apply did not
  already restart. Restart that service (usually `mngr start --restart
  system-services`, then `python3 system/scripts/refresh_workspace_view.py` so
  open views reload; skip both when the apply already restarted).

  This is the one land-then-activate gap the apply deliberately keeps. A
  supervisord-programmed service (`system/services/**`) goes on running its
  pre-merge code until its program restarts, and the apply does not restart it:
  the only restart it owns is the services agent's, which bounces *every*
  program and blips the user's UI -- far too blunt for, say, a backup-service
  change. Activating these precisely means restarting the individual programs a
  change actually touches, which the apply cannot infer from paths alone; until
  it can, that judgement is the report's and the restart is yours. Say so to
  the user rather than implying the merge alone made it live.
- **`Dockerfile`** -- apply the live-applicable hunks the report calls out
  (canonically a `CLAUDE_CODE_VERSION` bump -> `CLAUDE_CODE_VERSION=<v> bash
  system/scripts/setup_system.sh`, keeping `agent_types.claude.version` in
  `.mngr/settings.toml` in sync). Tell the user any image-level hunk (base
  `FROM`, `apt-get` packages, build-time layout) needs a manual workspace
  rebuild.
- **Rebuild-only flags** -- anything the report classified rebuild-only (a
  `build_arg` / `start_arg` / runtime flag, a user-created dependent of a
  global bump) is surfaced to the user as needing a workspace recreate; never
  imply it is already live.

### If the apply rolled back

An exit-2 rollback restored the workspace, but the user still asked for an
update they did not get -- so the message you compose (per the §5a rules)
must carry three things, in this order: **the workspace is safe** ("the update
hit a problem while being applied, so I automatically undid it -- everything
is back exactly as it was, and nothing is broken"); **what failed**, in plain
terms, at whatever level of cause the stderr established; and **the way
forward**. The retry path survives every rollback by design: the worker's
branch, worktree, and report are all kept, so once the failure is diagnosed
the re-land is quick -- offer exactly that ("I can look into what went wrong
and try again once it's fixed"), and never make the user feel the whole pass
must be redone from scratch. If the closing line said the frontend was already
broken beforehand, report that as its own problem alongside.

### Rolling back on request

The results message always offers a rollback, and the offer must be real. If
the user wants the update (or one piece of it) gone: create a **forward
revert** on a branch -- `git revert -m 1 <merge sha>` for the whole update, or
a commit reverting just the paths they dislike; never rewind history -- and
land it with the same machinery, `update_self.py apply --merge-ref <that
branch>` (ordinary merge mode, no `--target-ref`), so the revert gets the same
refresh, restart, and health-probe motion the update got. Two residues to
mention when they matter: the apt snapshot advanced by `env-converge upgrade`
stays advanced, and the version-history entry stays (the revert is its own
history). The full-rewind fallback is the Step 1 backup, when one was
captured.

## Migration-required updates

Some updates cannot be applied in place -- the judgment is yours, standardized
here rather than by any mechanical marker: the release restructures something
this workspace's live state was built on (a changed data layout with no
in-place migration, a provisioning change only a fresh container can adopt
that the workspace genuinely needs), or the worker's `stuck` report shows the
merge cannot land without breaking the running workspace.

When that is the verdict, keep the user-facing message high-level -- no path
tables, no hand-rolled migration plans:

> This update contains fundamental changes that can't be directly applied to a
> running workspace. To take it: (1) create a new workspace, then (2) message
> its agent `/migrate-workspace <this workspace's name>` -- it will pull your
> work, apps, and settings across.

If a newer in-place-compatible release **also** exists (the incompatibility
starts at some later version), offer both in the same breath: "I can apply
<X> now; <Y> needs the fresh-workspace migration." Resolve targets with
`--override` to land the compatible one if the user takes that option.

## 6. Teardown

**Tear down any stray preview.** This pass no longer creates previews, but an
interrupted older pass or another flow may have left one registered, and
`update-system-interface`'s preview guard refuses the next pass while one is.
If a system-interface preview is up:

```bash
python3 .agents/skills/update-system-interface/scripts/reveal_system_interface.py unpreview --slug update-self
python3 system/scripts/layout.py close si-preview
```

and for a preview of any other service, stop its isolated instance and close
its tab (`python3 system/scripts/layout.py close <name>`).

**The rest of this section is only for a successful apply (exit 0).** After a
rollback the retry path is the worker: its branch, worktree, and report are
what make a diagnosed retry a quick re-land, so do not stop the worker or
consume the report until the retry is resolved with the user (release the
leases either way, so another pass is not blocked while you wait). Then
consume the report and **stop** the worker -- do not destroy it:

```bash
mkdir -p data/.tasks/update-self/reports/consumed
mv data/.tasks/update-self/reports/report.md \
    data/.tasks/update-self/reports/consumed/$(date +%s)-done.md
mngr stop update-self
```

Stopping rather than destroying is deliberate. The worker's transcript is the
primary evidence when an update pass later turns out to have gone wrong, and
`mngr destroy` is the one thing that puts it out of reach of the bug-report
collector (after a destroy, the only surviving copy is the harness's raw log,
which nothing can find by asking mngr). A stopped worker consumes no memory,
stays listed, and its transcript stays readable through `mngr event`; the next
pass's launch clears it with `--destroy-existing` (Step 3b) before creating
the new one.

Consuming the terminal report is not optional bookkeeping: `create_worker.py
launch` refuses to start a worker while a leftover report sits at the report
path (a stale one would satisfy the next pass's `await` instantly), so skipping
this breaks the next update-self pass until someone cleans it up.

Release the leases and close the tracking ticket last (each its own tool call,
nothing chained): `tk close` the `editing service system_interface` lease if
5b took one, then the `updating workspace` lease
(`tk close "$UPDATE_LEASE_ID" "Update pass finished."`), then:

```bash
tk close <ticket-id> "Updated to <ref> -- worker branch merged and applied."
```

## To push local improvements back upstream

Use the `submit-upstream-changes` skill -- the complementary direction. This skill
only pulls.

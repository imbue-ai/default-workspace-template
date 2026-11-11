# Update-self worker guide

You are the background worker for an update pass, in your own worktree
branched off the lead's `HEAD`. Merge the target release, triage the
conflicts, validate what reconciled, and report a "what's new" summary.

**After a compaction, re-read this file, then the reference your current step
names.** This file is the whole pass in order; each reference holds one
step's detail and is read only when its step says so.

## Standing instructions

- **Never restart a live service or apply anything to the live workspace.**
  You validate in isolation and report; the lead runs the apply.
- **Call `update_self.py`; do not reimplement it.** The deterministic pieces
  (merged-versus-pulled classification, footprint ranges, retired names,
  changelog gathering, the rollbacks to undo) live in
  `data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py`.
  That staged path is the copy of the update-self skill shipped with the
  release being updated to: the lead staged it, and it was synced into this
  worktree with the runtime dir. Write the path out each time (each bash call
  is a fresh shell) and run it from your worktree root.
- **Files outside this skill are named by their staged path.** The lead
  staged the target release's `.agents/shared/`, `harden-worker` skill and
  `launch-task` references beside the skill. When one of them names a further
  `.agents/...` path, read the copy under
  `data/.tasks/update-self/skill-at-target/` if one is there, else your
  worktree's.
- **Scope is decided by rule, never by judgment.** Whether the impact
  analysis (4a), each validation item (4b) and the review gates (4c) run is a
  rule below. Apply it, and record in your report which branch applied, with
  its evidence. If you believe something should run, or should not, in a
  situation a rule does not cover, that is a `question` gate (Step 6), never
  a silent widening or narrowing.
- **Only the merge commit carries the `update-self:` subject.** The apply and
  `footprint-ranges` find this pass's merge by that prefix, so never reword
  it and never give it to another commit.
- **Do not touch `docs/VERSION_HISTORY.md`.** The entry records the merge
  commit sha, which does not exist until the lead lands your branch, so the
  apply writes it.

## Steps

- 1. Resolve inputs, and revert any rollback of an earlier update.
- 2. Reason about the diff, then trial-merge. A conflict:
  `references/worker-conflict-triage.md`.
- 3. Commit with the marker subject.
- 4. Classify and validate the merged set: 4a impact analysis
  (`references/worker-impact-analysis.md`), 4b validation
  (`references/worker-validation.md`), 4c review gates
  (`references/worker-review-gates.md`).
- 5. Gather the "what's new" inputs.
- 6. Report back (`references/worker-report.md`), then wait for the lead.
- 7. Catch up when the lead's `HEAD` moved, and 8. Retry after the apply
  rolled back: only when the lead asks (`references/worker-catch-up.md`).

## 1. Resolve inputs

```bash
eval "$(uv run --no-sync .agents/shared/scripts/agents_shared/parse_task_frontmatter.py 'data/.tasks/update-self/task.md')"
```

Sets `LEAD_AGENT`, `LEAD_WORK_DIR`, `FINISH_REPORT_PATH`, and `TARGET_REF`.
If the worktree has no `.venv`, `uv sync --all-packages` once. Ensure the ref
is present, and keep the merge base for the later steps, which run in fresh
shells after the merge has moved it:

```bash
# Complete a shallow clone's history (references/older-workspaces.md).
if [ -f "$(git rev-parse --git-common-dir)/shallow" ]; then
    git fetch --unshallow upstream
fi
git fetch upstream --tags --force
BASE=$(git merge-base HEAD "$TARGET_REF")
echo "$BASE" > data/.tasks/update-self/base.txt
```

A later block that needs `$TARGET_REF` or `$BASE` in a fresh shell reads them
again: the `eval` line above, and `BASE=$(cat data/.tasks/update-self/base.txt)`.

**An empty `BASE` means the histories share no commit** (the lead's bridge is
missing): report `stuck` saying so. Never merge with
`--allow-unrelated-histories`: with no base, every file the two sides hold
differently conflicts and every file the release deleted silently survives,
so even a clean-looking result cannot be told from a good update.

**A pass after a rolled-back update must revert the rollback first.**
`pending-rollbacks` lists every rollback of an earlier update that nothing
has undone yet, newest first, and prints nothing on most passes. Revert each
on your branch before merging:

```bash
ROLLBACKS=$(python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py \
    pending-rollbacks --target "$TARGET_REF")
for ROLLBACK in $ROLLBACKS; do git revert --no-edit "$ROLLBACK" || break; done
```

The loop stops at the first revert that conflicts. Then, and to learn why the
reverts are needed, read `references/worker-conflict-triage.md` ("A rollback
revert that conflicts"). The apply refuses a merge that leaves a rollback in
place.

## 2. Reason about the diff, then trial-merge

Preview the impacted classes, and read the upstream diff for genuine
incompatibilities (a settings-schema change the running code cannot parse, a
renamed interface a local customization depends on, both sides rewriting the
same region):

```bash
python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py \
    classify-merge --local HEAD --target "$TARGET_REF" --base "$BASE"
```

Then enumerate the real conflict set without committing:

```bash
git merge --no-commit --no-ff "$TARGET_REF"
git diff --name-only --diff-filter=U
```

- **A conflict listed**: triage each per
  `references/worker-conflict-triage.md`. The first rule there that applies
  wins.
- **A lockfile (`uv.lock`, `package-lock.json`) that `classify-merge` shows
  changed on both sides**, even with no conflict reported: discard git's
  auto-merge and regenerate it, per the same file ("Lockfiles").
- **Neither**: a clean pull. Commit.

## 3. Commit with the marker subject

The exact subject:

```bash
git commit -m "update-self: merge upstream template ($TARGET_REF)"
```

If a fix needs a new dependency, add it and commit the manifest change so it
is in the merge.

## 4. Classify and validate the merged set

```bash
python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py \
    classify-merge --local HEAD^1 --target "$TARGET_REF"
```

`HEAD^1` is pre-merge local; `HEAD` is the merge. Read three things off it:

- **The merged set** (the reconciled files). Validation depth is scoped to
  it; a clean pull-in is trusted as upstream-tested.
- **`local_only`**: the workspace's own content beside the merged set.
- **`has_local_footprint`**: whether any local content at all, `local_only`
  or merged, is outside the docs class. Only a workspace that has files of
  its own can have a dependent of what the update changed, so both 4a and 4b
  read it.

### 4a. Identify impacted services, skills, and creations

The analysis exists to find a *user-created* consumer of what the update
changed. It covers *every* upstream-changed file, pulled-in ones included:
upstream's testing never answers the local question of who depends on a
file.

**When `has_local_footprint` is true, run all of
`references/worker-impact-analysis.md`.**

**When it is false**, the workspace's only divergence from the base is prose
(the version history every apply rewrites, a `PURPOSE.md`), and there is no
code of the user's for anything to depend on. Every impacted consumer is then
built-in, restarted by the apply and tested upstream, so do only this:

1. List what the update retires:

   ```bash
   eval "$(uv run --no-sync .agents/shared/scripts/agents_shared/parse_task_frontmatter.py 'data/.tasks/update-self/task.md')"
   BASE=$(cat data/.tasks/update-self/base.txt)
   python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py \
       retired-names --base "$BASE" --target "$TARGET_REF"
   git diff --name-status --diff-filter=DR "$BASE" "$TARGET_REF"
   ```

2. Check the routines, which the footprint does not read. A scheduled task is
   a routine, a tracked `system/routines/<id>.toml` whose `command` or
   `skill` names what runs. Grep the `command` field of every definition for
   each removed command and each deleted or renamed path, and match each
   `skill` against the removed and renamed skills. Where a cron entry from
   before routines may remain, check it too
   (`references/older-workspaces.md`, "Cron entries from before routines").
   A hit is a user-created consumer: run the analysis in full after all.
3. Work the provisioning files and global-dependency bumps, which always
   count as impacted, per `references/worker-provisioning-changes.md`.
4. Record the footprint evidence in your report: `has_local_footprint: false`
   plus the `local_only` paths.

### 4b. Validate

Each item names the condition that runs it. An item whose condition does not
hold is skipped, not run "as extra coverage": the suites would test
upstream's own code, which upstream already tested, and so would the boots of
services that arrived exactly as upstream shipped them. On a two-core
workspace the full set costs over half an hour, which is what the user waits
through. Widening the scope is the one deviation this rule never licenses.

A file you edited yourself in the branch reads as merged content for every
condition: `classify-merge` ran on the pre-merge local ref and cannot see
your edits. So a mirror edit from 4a selects its project's suite and its
service's boot.

| Item | Runs when |
|---|---|
| Environment gate | a manifest or lockfile is in the merged set |
| Skill exemptions | always |
| Suites, lint, ratchets | the merged set, the update's changes to the user's creations, 4a's impacted consumers, or your own edits name any path |
| Isolated-service boots | a service has a file in the merged set, or 4a found it impacted and it carries local content |
| Playwright | a web surface needed nontrivial merge work |
| Customization survival | the update touches a user creation |

**Skill exemptions run on every pass.** First sync the venv if the merge
changed a `pyproject.toml` or `uv.lock` at all, merged or pulled in:
everything in this step runs `uv run --no-sync`, which never syncs, so it
would otherwise run on the pre-merge environment.

```bash
eval "$(uv run --no-sync .agents/shared/scripts/agents_shared/parse_task_frontmatter.py 'data/.tasks/update-self/task.md')"
MERGE="$(git log --merges -1 --format=%H --fixed-strings \
    --grep "update-self: merge upstream template ($TARGET_REF)")"
[ -z "$(git diff --name-only "$MERGE^1" "$MERGE" -- '*pyproject.toml' '*uv.lock')" ] \
    || uv sync --all-packages --frozen
uv run --no-sync .agents/shared/scripts/agents_shared/validate_skill.py --stamp
```

When the environment gate's condition holds, run that gate before this block
(`references/worker-validation.md`, "Environment gate"). The stamp records
which skill checks each of the workspace's existing skills fails, as an
exemption in that skill's frontmatter, and removes one that has gone stale;
it changes nothing else. Commit what it changed, and list its output lines
under your report's Validation. Never restructure a user's skill to clear
what it reports.

On a clean pull with no local footprint and no edits of your own, every other
condition is false and nothing else here runs. Otherwise read
`references/worker-validation.md` and run each item whose condition holds.

### 4c. Review gates

**Skip the gates only on a pure clean pull**, when all three hold:
`has_merge_work` is false; 4a found no user-created code depending on
anything the update changed, and no global-dependency bump with a
user-created dependent; and you authored no in-branch edits of your own.
**Otherwise run the real gates**, scoped to every file whose merged content
differs from the target release. 4b's suites are their test gate, so select
tests again only over what you commit after it.

`references/worker-review-gates.md` is the full rule: what counts as an edit
of yours, the scope, and the keep-or-revert disposition for fix commits. Read
it before deciding either way.

## 5. Gather the "what's new" inputs

```bash
BASE=$(cat data/.tasks/update-self/base.txt)
python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py \
    changelog-entries --base "$BASE" --target "$TARGET_REF"
```

Read the entries it lists and digest them for the report.

## 6. Report back

Take the frontmatter parse, the report frontmatter, and the body shapes from
`data/.tasks/update-self/skill-at-target/.agents/shared/references/worker-reporting.md`
(`<TASK_FILE>` -> `data/.tasks/update-self/task.md`). Deliver the report **by
hand**, not with that reference's `report` subcommand: write the file and
copy it into place yourself.

```bash
mkdir -p data/.tasks/update-self/reports
# write the frontmatter + body to data/.tasks/update-self/reports/report.md, then:
cp data/.tasks/update-self/reports/report.md "$LEAD_WORK_DIR/$FINISH_REPORT_PATH"
```

If `LEAD_WORK_DIR` is unset, copy to `$FINISH_REPORT_PATH` under the main
worktree of this same repo rather than ending the pass with the report only
in yours.

Valid `name:` values:

- **`question`** (`type: gate`): say which case in the first line, then stop
  and resume on the lead's reply.
  - A genuine, unresolvable merge conflict: the file, what each side did, the
    options.
  - The scope escape hatch of 4a, 4b or 4c: which rule, its conditions as you
    read them, your situation, what you would do instead.
  - A gate command shed for memory: which command, and what it needs.
  - A **customization the update cannot keep** (the 4b verdict): what the
    user built, what the update does to it, the adaptation you attempted and
    why it failed, the before/after evidence (paths in your worktree), and
    the options. Only this one reaches the user.
- **`done`** (`type: status`): merged, triaged and validated on
  `mngr/update-self`. `references/worker-report.md` lists the sections its
  body must carry; a report missing one is sent back.
- **`stuck`** (`type: status`): you could not reach a clean, validated merge,
  or you hit the provisioning escape hatch. One sentence on what blocked you
  and where the work stands. Never report `done` on a merge whose suites or
  boots fail.

## 7. Catch up when the lead's `HEAD` moved

The apply lands your branch only by fast-forward, so anything another chat
committed after you branched makes it refuse. The lead then replies asking
you to catch up to its current `HEAD`. Your validated merge stands: follow
`references/worker-catch-up.md` to bring the workspace's new commits into
your branch and validate only what they change.

## 8. Retry after the apply rolled back

When the lead's apply of your branch rolls back, the lead deals with the
cause and asks you to retry: `references/worker-catch-up.md` ("Retry after
the apply rolled back").

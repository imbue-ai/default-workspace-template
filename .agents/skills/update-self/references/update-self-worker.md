# Update-self worker guidance

You are the background worker for a safe update-self pass, in your own worktree
branched off the lead's `HEAD`. Merge the target upstream ref, triage the
conflicts, validate what reconciled, and report a "what's new" summary. **You
never restart a live service or apply anything to the live workspace** -- you
validate in isolation and report; the lead runs the apply.

The deterministic pieces (target resolution, merged-vs-pulled classification,
changelog gathering) live in
`data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py`
-- call it, don't reimplement. That staged path is the copy of the update-self
flow shipped with the version being updated to (the lead staged it and it was
synced into this worktree with the runtime dir); reference it by literal each
time (each bash invocation is a fresh shell) and run it from your worktree
root. Impact analysis (who depends on a changed file) is deliberately *not*
scripted; Step 4a is your recipe for it.

## 1. Resolve inputs

```bash
eval "$(uv run .agents/shared/scripts/parse_task_frontmatter.py 'data/.tasks/update-self/task.md')"
```

Sets `LEAD_AGENT`, `FINISH_REPORT_PATH`, and `TARGET_REF`. If the worktree has
no `.venv`, `uv sync --all-packages` once. Ensure the ref is present:

```bash
# Complete a --depth 1 clone's history first (a no-op elsewhere); see
# references/heal-shallow-history.md.
if [ -f "$(git rev-parse --git-common-dir)/shallow" ]; then
    git fetch --unshallow upstream
fi
git fetch upstream --tags
BASE=$(git merge-base HEAD "$TARGET_REF")
```

## 2. Reason about the diff, then trial-merge

Preview the impacted classes and read the upstream diff for genuine
incompatibilities (a settings-schema change the running code can't parse, a
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

Triage each conflict; the first rule that applies wins:

- **Generated lockfiles (`uv.lock`, `package-lock.json`) -> regenerate, never
  side-pick or hand-merge.** Resolve the corresponding manifest first, then
  regenerate from it (`uv lock` in the lock's directory; `npm install
  --package-lock-only` for npm) and `git add` the result.
- **Agent-owned files -> keep local** (`PURPOSE.md`, `data/`):
  `git checkout --ours -- <path> && git add <path>`.
- **Mixed files (`CLAUDE.md` and similar) -> merge by judgment.** Keep the
  agent-specific customizations, fold in the upstream additions. Side-picking
  one side of a code file outside the agent-owned rule is a last resort, and
  "ours is a superset" is a claim you must **verify, not assert**: diff the
  discarded side against the base and account for every change in it --
  present in the kept version, or knowingly dropped and named as dropped in
  your report. A wholesale side-pick still goes through the 4c review gate.
- **Files untouched locally -> take upstream**: `git checkout --theirs --
  <path> && git add <path>`.
- **No clear resolution -> gate, as a last resort.** Only after you have
  genuinely tried to reconcile (both rewrote the same region incompatibly and
  the answer depends on intent you cannot see) write a `name: question` gate
  (Step 6) describing the file, what each side did, and the options; push and
  stop. The lead decides (defaulting to this workspace's current behavior)
  and replies; apply the reply and continue.

**Lockfiles need attention even without a conflict.** Git auto-merges two
divergent `uv.lock`s into a semantically invalid file (duplicate `[[package]]`
entries), which has bricked a live workspace before. When `classify-merge`
shows a lockfile changed on **both** sides relative to the base, discard the
auto-merge and regenerate before committing, even with no conflict reported.

No conflicts at all -- after the lockfile check -- means a clean pull; commit.

## 3. Commit with the marker subject

Commit with the exact subject (tools classify built-in code by the
`update-self:` prefix -- never reword it):

```bash
git commit -m "update-self: merge upstream template ($TARGET_REF)"
```

If a fix needs a new dependency, add it and commit the manifest change so it
is in the merge. **Do not touch `docs/VERSION_HISTORY.md`**: the entry records
the merge commit sha, which does not exist until the lead lands your branch,
so the apply writes it.

## 4. Classify and validate the merged set

```bash
python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py \
    classify-merge --local HEAD^1 --target "$TARGET_REF"
```

`HEAD^1` is pre-merge local; `HEAD` is the merge. **Validation depth is scoped
to the merged set** (the reconciled files); a clean pull-in is trusted as
upstream-tested. The impact analysis below covers *every* upstream-changed
file, pulled-in ones included: upstream's testing never answers the local
question of who depends on the file.

### 4a. Identify impacted services, skills, and creations

Exploration work, for every changed `system/scripts/**`, `system/libs/**`,
`system/services/**`, `system/apps/**`, `system/vendor/**`, and `.agents/**`
path. A move of the mngr pin (`pyproject.toml`, `[tool.uv.sources]`) is the
largest surface an update moves; mngr's changelog entries between the two
commits announce removed behavior in prose that no test failure surfaces --
read them (in the public repo at https://github.com/imbue-ai/mngr) and grep the
workspace for every name they retire (an environment variable, a port, a
command).

1. **Enumerate the consumer universe** up front: every `system/supervisord.conf`
   program (and what its `command` invokes), every app or service under
   `system/services/` and `system/apps/`, every workspace-added skill under
   `.agents/skills/`, and any cron or scheduled runners.
2. **Search for dependents of each changed file**: its path, basename, and
   importable module name; follow each service's code into the shared
   scripts and libs it calls; check skills' `SKILL.md` and scripts.
3. **Reason about interface-level coupling no grep finds**: an API surface (the
   system interface HTTP API, a shared data file's format, a script's CLI
   flags) has callers that reference no file of it.
4. **Bias toward "impacted" when uncertain**, and record what you checked and
   how, so the lead sees the coverage.
5. **Verify "workspace-added" by provenance, not directory**: a path is
   built-in if it exists at the target ref (`git ls-tree -r --name-only
   "$TARGET_REF" -- <dir>`; empty output = genuinely workspace-added).
   Mislabeling built-in code as the user's misattributes pre-existing issues.

The apply restarts the services agent (every supervisord program) on every
apply, so built-in services need no naming for a restart; what only your
analysis can name is a *user-created* consumer -- an app, widget or skill of
the user's that reads something the update changed. An impacted service still
gets validated below; an impacted skill gets validated per its own contract.

**Provisioning files** (`system/scripts/setup_system.sh`, the installers it
chains, `.mngr/**`) and **global-dependency bumps** always count as impacted
even though nothing imports them; work them per
`references/worker-provisioning-changes.md` and report each as
live-applicable, rebuild-only, or `stuck`.

### 4b. Validate

- **Environment gate first**, whenever a manifest or lockfile is in the
  merged set: `uv lock --check` then `uv sync --all-packages`. A failure here
  is a precise blocker (an unparseable root lock means no service in the
  workspace can start); fix it before running anything else.
- **Suites, lint, ratchets** for each project in `projects_to_validate`: root
  `.` (`uv run pytest` + `uv run ruff check`); `system/apps/system_interface`
  its own `uv run pytest` (and `npm run lint && npm run test` when the
  frontend merged). mngr's own suite runs in its repo, not here.
- **Isolated-service boots** for each impacted service, against a scratch
  data copy via `.agents/shared/scripts/serve_isolated_instance.py` (see
  `update-app`), never the live store. This runs on the host's global
  toolchain, so it does not exercise a global-dependency bump.
- **Playwright** for a web surface (system interface or a user service) only
  when the merge needed nontrivial merge work there. For the system interface,
  build it in your worktree (`uv sync --all-packages`, then `cd
  system/apps/system_interface/frontend && npm ci && npm run build`) and drive
  it per `.agents/shared/worker/references/web-frontend-testing.md`. That
  bundle is what the lead's apply installs live (`--worker-bundle`) -- name its
  location in your report.
- **Customization survival** -- for every user creation the update touches
  (workspace-added apps, widgets and skills; user-modified built-in surfaces;
  apps hooking into the system interface's API or state), verify the *merged
  result* still carries it in substance. Suites passing is not the bar. For a
  visual surface, screenshot the merged instance you booted and the running
  workspace's surface (read-only) for the before picture, and actually look at
  the pair; for an app or integration, exercise its hook points. Classify
  each: **intact**; **intact-but-changed** (moved, restyled -- never blocks;
  record it with the before/after evidence so the lead can offer to restore
  the old arrangement); **cannot be kept** (no place for it on the new base,
  or broken and your attempts to re-fit it failed -- "tried and failed", never
  "looks hard"). Cannot-be-kept is the one verdict that stops the pass: raise
  it as a `question` gate (Step 6) with the evidence and options; never let it
  ride into `done`. A conflict where every resolution breaks the creation
  lands here too.

### 4c. Review gates

Whether the gates run is decided by a rule, not by your judgment; apply it and
record which branch applied, with its evidence, in your report. **Skip the
gates only on a pure clean pull**: `has_merge_work` false from Step 4 **and**
your 4a analysis found no user-created code depending on anything the update
changed (and no global-dep bump with a user-created dependent) **and** you
authored no in-branch edits of your own (a mirror edit from 4a is merge work
even though `classify-merge` cannot see it). **Otherwise run the real gates**,
scoped to every file whose merged content differs from the target release. The
full rule, its scope, and the keep/revert disposition for fix commits are in
`references/worker-review-gates.md`. If you believe the gates should not run,
or should run at another scope, in a situation the rule does not cover, that is
a `question` gate for the lead -- never a silent adaptation.

## 5. Gather the "what's new" inputs

```bash
python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py \
    changelog-entries --base "$BASE" --target "$TARGET_REF"
```

## 6. Report back

Per `.agents/shared/references/worker-reporting.md` (`<TASK_FILE_GLOB>` ->
`data/.tasks/update-self/task.md`; `<RUNTIME_REPORTS_DIR>` ->
`data/.tasks/update-self/reports`). Valid `name:` values:

- `question` (`type: gate`) -- three cases; say which in the first line.
  (a) A genuine, unresolvable merge conflict: the file, what each side did,
  the options. (b) The 4c review-gate escape hatch: the rule's conditions as
  you read them, your situation, what you would do instead. (c) A
  **customization the update cannot keep** (the 4b verdict): what the user
  built, what the update does to it, the adaptation you attempted and why it
  failed, the before/after evidence (paths in your worktree), and the options.
  Only (c) reaches the user. Push and stop; resume on the lead's reply.
- `done` (`type: status`) -- merged, triaged, validated on `mngr/update-self`.
  The body gives the lead everything for the audit, the apply, and the results
  message:
  - **What's new** -- a digest of the changelog entries.
  - **Conflicts** -- each one and its resolution. For any side-pick, or claim
    that the kept side subsumes the other, list what the discarded side changed
    and where each change ended up, from the base-diff accounting of Step 2.
  - **Merged vs pulled-in** -- which change classes reconciled vs came in clean.
  - **Merge work per web surface** -- "none" or "nontrivial" (with a sentence
    on what was reconciled) for the system interface and each user web
    service; the lead attaches the rollback offer to each nontrivial one.
  - **Customization survival** -- each touched creation classified per 4b,
    with evidence paths for anything not plainly intact.
  - **Built system-interface bundle** -- when you built it, the absolute path
    (`<your work_dir>/system/apps/system_interface/imbue/system_interface/static`);
    omit when you did not build.
  - **Impact analysis** -- what you checked and how, and any user-created app
    or skill depending on a changed file.
  - **Dockerfile split** (if it merged) -- each hunk live-applicable or
    image-level. Version pins live in `setup_system.sh`, so a pin bump is a
    provisioner change, not a Dockerfile hunk.
  - **Provisioning changes** and **global-dependency bumps** (if any) -- per
    `references/worker-provisioning-changes.md`: each classified
    live-applicable (naming the in-branch mirror edits you made) or
    rebuild-only, with the version delta and what your research turned up; a
    genuinely breaking, unapplyable change is a `stuck` report, not a `done`.
  - **Validation** -- suites, boots and Playwright run, all passing; **which
    branch of the 4c rule applied, with its evidence** (the clean-pull skip's
    three conditions, or the gate run's kept/reverted fix commits -- or "gate
    ran clean" -- and the architecture-gate verdicts); any validation gap
    called out honestly. A report with neither record is incomplete and the
    lead sends it back.
- `stuck` (`type: status`) -- you could not reach a clean, validated merge, or
  you hit the provisioning escape hatch; one sentence on what blocked you and
  where the work stands. Never report `done` on a merge whose suites or boots
  fail.

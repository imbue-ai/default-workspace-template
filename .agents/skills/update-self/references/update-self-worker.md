# Update-self worker guidance

You are the background worker for a safe update-self pass, in your own worktree
branched off the lead's `HEAD`. Merge the target upstream ref, triage the
conflicts, validate what reconciled, and report a "what's new" summary. **You
never restart a live service or apply anything to the live workspace** -- you
validate in isolation and report; the lead runs the apply.

The deterministic pieces (target resolution, merged-vs-pulled classification,
changelog gathering) live in
`data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py`
-- call it, don't reimplement. That
`data/.tasks/update-self/skill-at-target/.agents/skills/update-self` path is the copy
of the update-self flow shipped with the version being updated to (the lead staged
it and it was synced into this worktree with the runtime dir); running from it
means you use the target version's flow, not this worktree's possibly-stale copy.
Impact analysis (who depends on a changed file) is deliberately *not* scripted;
Step 4a is your recipe for it.

## 1. Resolve inputs

```bash
eval "$(uv run .agents/shared/scripts/parse_task_frontmatter.py 'data/.tasks/update-self/task.md')"
```

Sets `LEAD_AGENT`, `FINISH_REPORT_PATH`, and `TARGET_REF`. Run every
`update_self.py` call below from
`data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/` (a fixed
path -- reference it by literal each time rather than stashing it in a shell
variable, since each bash invocation starts a fresh shell). If the worktree has no
`.venv`, `uv sync --all-packages` once. Ensure the ref is present:

```bash
# Heal a shallow-history workspace (created from a pool host baked with a
# --depth 1 clone) before fetching: complete the history from the public
# upstream so `git log` / merge archaeology work. Your worktree shares the
# main repo's object database and its repo-wide shallow marker (in the git
# COMMON dir -- hence `--git-common-dir`), so this heals the whole workspace,
# and it runs from the target version's copy of this guide, so the heal
# applies on the first update into the release that shipped it. The guard
# keeps it a no-op on healthy repos (`--unshallow` errors when not shallow).
if [ -f "$(git rev-parse --git-common-dir)/shallow" ]; then
    git fetch --unshallow upstream
fi
git fetch upstream --tags
BASE=$(git merge-base HEAD "$TARGET_REF")
```

## 2. Reason about the diff, then trial-merge

Preview the impacted classes and read the upstream diff for genuine
incompatibilities (a settings-schema change the running code can't parse, a
renamed interface a local customization depends on, both sides rewriting the same
region) -- so you can frame a precise `question` before committing to the merge:

```bash
python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py \
    classify-merge --local HEAD --target "$TARGET_REF" --base "$BASE"
```

Then enumerate the real conflict set without committing:

```bash
git merge --no-commit --no-ff "$TARGET_REF"
git diff --name-only --diff-filter=U
```

Triage each conflict, first rule that applies wins:

- **Generated lockfiles (`uv.lock`, `package-lock.json`) -> regenerate, never
  side-pick or hand-merge.** If both sides' manifests changed, *neither* side's
  lock matches the merged manifest, and hand-editing conflict markers in a lock
  produces a file the tool can't parse. Resolve the corresponding manifest
  first, then regenerate from it (`uv lock` in the lock's directory; `npm
  install --package-lock-only` for npm) and `git add` the result.
- **Agent-owned files -> keep local** (`PURPOSE.md`, `data/`):
  `git checkout --ours -- <path> && git add <path>`.
- **Mixed files (`CLAUDE.md` and similar) -> merge by judgment.** Do not blanket
  keep-local: upstream additions (new sections, updated shared guidance) are
  often worth integrating. Resolve by editing the file -- keep the
  agent-specific customizations, fold in the upstream additions. Side-picking
  one side of a code file (`git checkout --ours`/`--theirs` outside the
  agent-owned rule above) is a last resort, and "ours is a superset" is a claim
  you must **verify, not assert**: diff the discarded side against the base and
  account for every change in it -- either present in the kept version, or
  knowingly dropped and named as dropped in your report. A wholesale side-pick
  is still a resolution of the merged set, so the kept file goes through the
  4c review gate like any hand-edit.
- **Files untouched locally -> take upstream**: `git checkout --theirs -- <path>
  && git add <path>`.
- **No clear resolution -> gate, as a last resort.** Only after you have
  genuinely tried to reconcile and found no resolution that preserves both
  sides' intent (both rewrote the same region incompatibly and the answer
  depends on intent you cannot see) do you write a `name: question` gate
  (Step 6) describing the file, what each side did, and the options; push and
  stop. The lead decides -- the flow is unattended, and its default is to
  preserve this workspace's current behavior, with the decision reported to
  the user afterwards -- and replies; apply the reply and continue. Never gate
  on anything the rules above or reasonable judgment can settle.

**Lockfiles need attention even without a conflict.** Git will happily
auto-merge two divergent `uv.lock`s into a semantically invalid file (duplicate
`[[package]]` entries uv then can't disambiguate) -- this has bricked a live
workspace before. When the Step 2 `classify-merge` shows a lockfile changed on
**both** sides relative to the base, discard git's auto-merge of it and
regenerate (`uv lock` / `npm install --package-lock-only`) before committing,
even when the merge reported no conflicts. (The repo's `.gitattributes` marks
these locks `merge=binary` so divergence *should* surface as a conflict, but do
not rely on it -- older local histories may predate that.)

No conflicts at all -- after the lockfile check above -- means a clean pull; go
straight to committing.

## 3. Commit with the marker subject

Once every path is resolved and staged, commit with the exact subject (tools like
`assist` classify built-in code by the `update-self:` prefix -- never reword it):

```bash
git commit -m "update-self: merge upstream template ($TARGET_REF)"
```

If a fix needs a new dependency, add it and commit the manifest change so it's in
the merge.

**Do not touch `docs/VERSION_HISTORY.md`.** The workspace's version entry records the
*merge commit sha*, which does not exist until the lead fast-forwards onto your
branch, so the apply script (`update_self.py apply`, run by the lead) writes it
as part of landing -- see the `update-self` skill's Step 5b. A line written here
would carry the wrong sha and would conflict with the apply's.

## 4. Classify and validate the merged set

Split what upstream changed into the reconciled **merged** set (validate) vs the
clean **pulled-in** set (trust as upstream-tested):

```bash
python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py \
    classify-merge --local HEAD^1 --target "$TARGET_REF"
```

`HEAD^1` is pre-merge local; `HEAD` is the merge. `projects_to_validate`,
`reveal_classes_merged`, and the per-file entries scope the work below.
**Validation depth is scoped to the merged set**; a clean pull-in is not
re-validated -- but the impact analysis below covers *every* upstream-changed
file, pulled-in ones included: trusting upstream's testing never answers the
local question of who depends on the file.

### 4a. Identify impacted services and skills

No script can enumerate what depends on a changed file -- this is exploration
work, and you must do it for every changed `system/scripts/**`, `system/libs/**`,
`system/services/**`, `system/apps/**`, and `.agents/**` path. Build the impact
set like this:

1. **Enumerate the consumer universe** up front, independent of the diff: every
   `system/supervisord.conf` program (and everything its `command` invokes, directly or
   through a wrapper), every app or service under `system/services/` and `system/apps/`, every workspace-added skill
   under `.agents/skills/` (e.g. a crystallized `fetch-process-show` pipeline
   whose scripts a daemon or scheduled job runs), and any cron/scheduled
   runners.
2. **Search for dependents of each changed file**: grep the repo for its path,
   its basename, and its importable module name; follow each service's code
   into the shared scripts and libs it calls; check skills' `SKILL.md` and
   scripts for references.
3. **Reason about interface-level coupling that no grep will find.** If the
   diff changes an API surface -- the system_interface HTTP API, a shared data
   file's format, a script's CLI flags -- ask who *calls* that surface: a local
   service built against the system_interface API is impacted even though no
   file of it references the changed one.
4. **Bias toward "impacted" when uncertain**, and record in your report what
   you checked and how, so the lead sees the coverage instead of trusting an
   unstated search.
5. **When you label a lib or skill "workspace-added," verify it -- do not infer
   it from the directory.** The layout is a strong hint (`system/libs/` and
   `system/services/` hold only built-in template packages; workspace-built
   apps land in `system/apps/` next to the built-in ones), but
   the check is provenance: a path is built-in if it exists at the target ref;
   check before labeling: `git ls-tree -r --name-only "$TARGET_REF" -- <dir>`
   (empty output = genuinely workspace-added). This matters because only
   genuinely workspace-added code is un-validated-by-upstream -- mislabeling
   built-in code as workspace-added misattributes pre-existing issues (a failing
   test or lint error) as the user's when they are the upstream release's, and
   the lead's results message repeats the error.

**Provisioning files always count as impacted -- and you best-effort apply them.**
A change to `system/scripts/setup_system.sh`, `system/scripts/install_secret_scanners.sh`,
`system/scripts/_provision_guard.sh`, or `.mngr/**` (the `provisioner` change class) has
no *running* consumer to grep for -- nothing imports it -- yet it installs and
configures the global toolchain (the latchkey CLI, uv, claude, modal, the secret
scanners) and the `mngr create` config every live agent, service, and future
sub-agent runs on. So never conclude "nothing to apply" for one. Work each
provisioning change through, most-live-applicable first, and record what you
found in your report (you stay in your worktree -- you make the in-repo edits
an apply implies; the apply script runs the provisioner and the restarts):

- **Toolchain-script pins** (`setup_system.sh` / `install_secret_scanners.sh`) --
  a pinned-version bump (e.g. `LATCHKEY_VERSION`) is **live-applicable**: the
  apply re-runs the idempotent provisioner (`bash system/scripts/setup_system.sh`),
  before any restart, to install the new version. A hunk only a fresh image
  build reproduces is **rebuild-only**.
- **`.mngr/**` settings** -- `.mngr/settings.toml` only governs `mngr create`, so
  the merged file governs every *future* create automatically (a new workspace,
  and the sub-agents `launch-task` spawns). But the *current* workspace was built
  and launched under the **old** settings, so a create-time change does not reach
  it on its own. **Examine each changed setting and best-effort make it live:**
    Lean hard toward applying live: most settings have a live counterpart, and
    "it's fiddly to get right" is not a reason to defer -- only a genuine lack of
    any live lever is.

    **Ground every apply in how `system/vendor/mngr` consumes the setting -- do not guess
    the live mechanism.** For each changed key, grep `system/vendor/mngr` for its name to
    find exactly where mngr reads and enacts it at create time, then mirror *that*
    mechanism. E.g. a `commands.create` `host_env__extend` change: `grep -rn
    host_env system/vendor/mngr` shows where mngr turns those entries into the agent
    container's environment (which env file / process env it writes), so you know
    the precise place to set them live and which process must restart to re-read
    them. Likewise `settings_overrides` -> where mngr writes Claude's settings;
    `extra_provision_command` -> how/when mngr runs it; `disable_plugin` -> where
    the plugin list is applied. Applying the setting the way mngr itself does is
    what makes the live edit correct rather than a plausible-looking guess.

    Cases, most-clearly-applyable first:
  - **Env vars and agent behavior** (`host_env` / `pass_env` / `pass_host_env` /
    `env`, `settings_overrides` like `model` / `fastMode`, `disable_plugin`) are
    **live-applicable**, just fiddly: they shape the environment and config that
    each agent/service process reads *at launch*, so mirror the change into the
    live equivalent (an env var into a `profile.d` entry or the relevant
    supervisord program's `environment=`; an agent-behavior override into whatever
    the running agent reads) and have the lead bounce the consumers -- `mngr start
    --restart system-services`, or a relaunch of the affected agent -- so the next
    process start picks it up. Do the mirror edit in your branch so it merges and
    is validated. Get it right rather than punting it to a rebuild.
  - A **toolchain/version pin** under `[agent_types.*]` (Claude version) -> mirror
    into `setup_system.sh` / the `Dockerfile` pin so a provisioner re-run installs
    it, and bounce the services agent. An `extra_provision_command` addition -> the
    lead runs that command live. Keep lockstep pins (`agent_types.claude.version`
    vs the Dockerfile `CLAUDE_CODE_VERSION` and the installed binary) consistent
    across every file that carries them.
  - Only a **container build/launch parameter** an already-running container
    genuinely cannot adopt -- a `[create_templates.*]` / `[providers.*]`
    `build_arg`, a `start_arg` (`--security-opt`, `--tmpfs`, `--workdir`,
    `--cpus`/`--memory`/`--disk`, `--restart`), or a runtime/provider flag (`runsc`
    / `docker_runtime` / `install_gvisor_runtime`) -- is **rebuild-only for the
    current workspace** (it still governs future creates). Flag it to the lead as
    needing a workspace recreate, exactly like an image-level `Dockerfile` hunk; do
    not imply it is already in effect.

**Escape hatch (`stuck`).** If a provisioning change is **not** live-applicable
**and** leaving the running workspace on the old provisioning would **genuinely
break it** (not merely "won't take effect until the next create"), do **not**
report `done` with a rebuild flag -- report `stuck` (Step 6), name the setting and
why it breaks, and refuse the update so the live workspace is left untouched.
Reserve this for real breakage; a change that is simply deferred-until-rebuild is
`done` plus a rebuild flag, not `stuck`.

**A global-dependency bump with a dependent -- safety turns on who depends on it.**
When a merge bumps a *global* dependency (a `setup_system.sh` /
`install_secret_scanners.sh` pin, or a `Dockerfile` toolchain pin), whether it is
safe to apply live depends on **who consumes the new version**. Your worktree
cannot itself validate the pair -- worktree isolation isolates the *repo tree*,
not the host-global toolchain, so your env still has the **old** dep; do **not**
globally install the new one to test, that mutates the shared toolchain the live
workspace and other agents run on. So decide by the **provenance** of the
dependent -- does its code come from the upstream template, or was it built in
this workspace? Decide this by *origin, not directory*: the layout is only a
hint (a workspace's own `build-app` app lands under `system/apps/`, and
the template's built-in services under `system/services/`, but an adapted
template can bring third-party creations along). The check is whether the
dependent's code exists in upstream at the target ref -- e.g. `git cat-file -e
"$TARGET_REF":<path>` for its files, or whether it's part of the merge base's
template rather than added locally.

- **Dependent is built-in code** (present in the upstream template at the target
  ref -- e.g. `system/apps/system_interface`, a template-shipped `system/services/*` service, a
  `.agents/shared/` script): **classify it live-applicable and report that** -- the
  upstream release tested that built-in code against the bumped dependency
  *together*, so it's safe to apply on the same "trust upstream's testing" basis
  the whole pulled-in set rides on. Not rebuild-only. **You do not run the bump
  yourself:** re-running the provisioner is a live, host-global toolchain mutation
  you can't (and mustn't) do from your worktree -- the apply does it. Your job
  here is only to judge it safe-to-apply and say so in the report; you don't
  validate the built-in against the new dep either, because you're trusting
  upstream's testing rather than re-doing it.
- **Dependent is user-created** (absent from upstream -- built in this workspace:
  a `build-app` app in its own `system/apps/` package, a crystallized skill's scripts
  under `.agents/skills/<skill>/`, a local script): **unsafe to hot-apply.**
  Upstream never saw that code, so it never tested it against the new dependency,
  and you can't either (shared toolchain). Classify it **rebuild-only** -- the safe
  way to land it is a workspace recreate, which provisions the new substrate and
  re-runs the user code against it. If leaving it unapplied would break the running
  workspace, that's `stuck`.

For either case:
- **Research the version change online** to ground your assessment: look up the
  dependency's release notes / changelog for the exact old -> new delta (breaking
  changes, removed flags, changed behavior, new minimum runtimes). Don't rely on
  memory -- fetch the actual notes and record what you found and how it bears on
  the dependent (this is what tells you whether a *user* dependent is likely fine
  or genuinely at risk).
- **Report the coupling** explicitly: which dependent, whether it's built-in or
  user-created, what you could/couldn't validate, and your apply / rebuild-only /
  `stuck` call.

An impacted *service* gets validated below (boot + suites) and flagged for
restart in your report. An impacted *skill* (a workspace-added skill relying on
something the update changed) gets validated per its own contract -- run its
tests, or exercise its scripts -- and called out in the report.

### 4b. Validate

- **Environment gate first**, whenever a manifest or lockfile is in the merged
  set (in particular after any lock you regenerated):

  ```bash
  uv lock --check          # lock parses and matches the merged pyproject
  uv sync --all-packages   # env actually builds from it
  ```

  A failure here is a precise blocker -- fix the lock/manifest before running
  anything else, or a corrupt or manifest-stale lock surfaces later as a
  confusing `uv run pytest` explosion. This maps 1:1 to the worst live failure
  mode: `bootstrap` is `uv run`-launched, so an unparseable root lock means no
  service in the workspace can start.
- **Suites/lint/ratchets** for each project in `projects_to_validate`: root `.`
  (`uv run pytest` + `uv run ruff check`) covers `system/libs/**`, `system/services/**`, `system/apps/**`, `system/scripts/**`,
  `.agents/**`; `system/apps/system_interface` runs its own `uv run pytest` (and `npm run
  lint && npm run test` when the frontend merged); `system/vendor/mngr` its own `uv run
  pytest`.
- **Isolated-service boots** for each impacted service (per 4a) -- boot against a
  scratch data copy via `.agents/shared/scripts/serve_isolated_instance.py` (see
  `update-app`), never the live store; a service that won't boot on the merged
  code is a blocker. Note this boot runs on the **host's global toolchain**, so it
  does *not* exercise a global-dependency bump -- a service coupled to one is the
  gap covered by the coupled-change note in 4a, not something an isolated boot
  can close.
- **Playwright** for a web surface -- system interface *or* a user service -- only
  when the merge needed nontrivial merge work there (not a clean pull). For the
  system interface, build it in your worktree (`uv sync --all-packages`, then
  `cd system/apps/system_interface/frontend && npm ci && npm run build`) so your
  work_dir is a built instance, then drive it per
  `.agents/shared/worker/references/web-frontend-testing.md`. That built bundle
  is also what the lead's apply installs live (`--worker-bundle`), so the exact
  build you validated is what ships -- name its location in your report
  (see §6). Your `npm ci` / `uv sync` runs here also pre-warm the shared uv and
  npm caches, which the live apply's own refresh then reuses, so the live
  motion is faster and less network-dependent than a cold one.
- **Customization survival** -- for every user customization the update touches
  (from 4a: workspace-added apps, widgets, and skills; user-modified built-in
  surfaces; and user-built apps that hook into the system interface's API or
  its state), verify that the *merged result* still carries it in substance.
  Suites passing is not the bar -- the user's thing still being there and
  working is, and a merge can be textually clean while functionally destroying
  it. For a visual surface, screenshot the merged instance you booted above
  and, for the before picture, the running workspace's surface (read-only) or
  an isolated instance of the pre-merge tree -- then actually look at the
  pair. For an app or integration, exercise its hook points against the
  merged instance. Classify each customization:
  - **intact** -- unchanged in look and behavior.
  - **intact-but-changed** -- still present and working, but moved, restyled,
    or otherwise cosmetically different (a widget in a new position). Never
    blocks; record it with the before/after evidence so the lead's results
    message can name it and offer to restore the old arrangement.
  - **cannot be kept** -- the update's new structure has no place for it, or
    it is functionally broken and your attempts to re-fit it on the new base
    failed. Reach this class only *after* genuinely trying to adapt the
    customization -- "tried and failed", never "looks hard". This is the one
    verdict that stops the pass: raise it as a `question` gate (Step 6) with
    the evidence and the options you see; never let it ride into `done` as a
    footnote.

### 4c. Review gates

Whether the gates run is decided by a rule, not by your judgment. Apply it and
record which branch applied (with its evidence) in your report:

- **Skip the gates only on a pure clean pull**: Step 4's `classify-merge`
  reports `has_merge_work` false (an empty merged set -- no conflicts, no file
  changed on both sides, no lockfile you regenerated) **and** your 4a impact
  analysis found no user-created code (apps, skills, local scripts) depending
  on anything the update changed and no global-dep bump with a user-created
  dependent **and** you authored no in-branch edits of your own -- a 4a mirror
  edit, or any other commit you added on top of the merge, is merge work even
  though `classify-merge` (which diffs `HEAD^1` against the base) cannot see
  it, and puts you on the run branch below. Every changed file then arrives
  exactly as upstream shipped and tested it, and there is nothing local for a
  review to protect. Running
  `/autofix` here would review *upstream's* code and could apply local fixes to
  it -- manufacturing exactly the local divergence a future update would have
  to reconcile -- so on a clean pull the skip is the correct outcome, not a
  shortcut. Your report states that this branch fired and shows the evidence for
  all three conditions: `has_merge_work: false`, an impact analysis with no
  user-created code in it (built-in impacts -- a service the lead must restart --
  do not block the skip), and no in-branch edits of your own.

- **Otherwise run the real gates, scoped to the locally-divergent content**:
  follow the "Review gates" section of
  `.agents/shared/worker/references/harden-creation.md` (unattended
  `/autofix`, then judge each fix commit yourself -- keep by default, revert
  only what undoes intended behavior -- plus the architecture gates). The
  gate's scope is **every file whose merged content differs from the target
  release**: the conflicts you resolved with any hand-written content, your
  own in-branch edits (a 4a mirror edit), and any lockfile you regenerated.
  That set, not the whole upstream diff, is what a review protects: a file
  byte-identical to the release arrived exactly as upstream tested it, and a
  fix to it would only manufacture local divergence (the disposition rule
  below reverts such fixes anyway). Over an 800-file release this is the
  difference between reviewing four reconciled files and reviewing
  upstream's code. Name the scope you ran in your report. Widening it is
  always allowed; narrowing it below that set, or substituting a review of
  your own design for the gates, is not -- this rule already *is* the
  proportionality decision. "The merge is dominated by upstream-tested code"
  licenses the skip branch above when its conditions hold, and licenses
  nothing when they do not.

  One disposition rule specific to update merges, for the keep/revert pass. The
  test is the file's merged **content**, not which set `classify-merge` put it
  in: **keep fixes to a file whose content differs from the release** -- one you
  reconciled by hand, or one you edited in-branch yourself (a 4a mirror edit)
  even though `classify-merge` lists it as pulled-in, since those edits are a
  reason the gate is running -- and **revert fixes to a file that is still
  byte-identical to the release** (note them as `submit-upstream-changes`
  candidates instead), including a conflicted file you resolved by taking
  upstream wholesale (`--theirs`), which lands byte-identical even though
  `classify-merge` lists it as merged. The gate's job here is the reconciliation
  and local breakage, not improving upstream's code.

If you believe the gates should not run -- or should run at some other scope --
in a situation this rule does not cover, that is a `question` gate for the lead
(Step 6), never a silent adaptation, however well-reasoned and however openly
you would have disclosed it. Record kept/reverted fixes and gate verdicts for
your report.

## 5. Gather the "what's new" inputs

```bash
python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py \
    changelog-entries --base "$BASE" --target "$TARGET_REF"
```

## 6. Report back

Per `.agents/shared/references/worker-reporting.md` (`<TASK_FILE_GLOB>` ->
`data/.tasks/update-self/task.md`; `<RUNTIME_REPORTS_DIR>` ->
`data/.tasks/update-self/reports`). Valid `name:` values:

- `question` (`type: gate`) -- three cases; say which it is in the first line.
  (a) A genuine, unresolvable merge conflict; body: the file, what each side
  did, the options. The lead answers this itself, defaulting to this
  workspace's current behavior. (b) The 4c review-gate escape hatch, a
  *process* question rather than a conflict; body: the rule's conditions as
  you read them, what your situation is, and what you would do instead. The
  lead answers it by the §4c rule. (c) A **customization the update cannot
  keep** (the 4b survival verdict); body: what the user built, what the update
  does to it, the adaptation you attempted and why it failed, the
  before/after evidence (paths in your worktree), and the options you see.
  This is the one case the lead escalates to the user, and the pass waits
  with the workspace untouched. Push and stop; resume on the lead's reply.
- `done` (`type: status`) -- merged, triaged, validated on `mngr/update-self`. Body
  gives the lead everything for the report audit, the apply, and the results
  message:
  - **What's new** -- a digest of the changelog entries.
  - **Conflicts** -- each one and how you resolved it. For any conflict where
    one side was taken wholesale (or where you claim the kept side subsumes the
    other), list what the discarded side changed and where each of those
    changes ended up -- present in the kept version, or dropped -- grounded in
    the base-diff accounting Step 2 requires, not asserted from memory.
  - **Merged vs pulled-in** -- which change classes reconciled vs came in clean.
  - **Merge work per web surface** -- for the system interface and each user web
    service: "none" (upstream strictly newer, clean pull) or "nontrivial" with a
    sentence on what had to be reconciled. The lead's results message points
    the user at each surface you judged nontrivial (with the rollback offer
    attached to it), so judge this explicitly.
  - **Customization survival** -- each user customization the update touches,
    classified intact / intact-but-changed / cannot-be-kept per 4b, with the
    before/after evidence paths for anything not plainly intact. A
    cannot-be-kept should already have gone out as a `question` gate; it
    never rides silently inside a `done`.
  - **Built system-interface bundle** -- when you built the system interface
    (for validation), the absolute path of the built bundle in
    your worktree
    (`<your work_dir>/system/apps/system_interface/imbue/system_interface/static`).
    The lead passes it to the apply as `--worker-bundle`, so the build you
    validated is the one installed live. Omit the field when you did not
    build (the apply falls back to a live build).
  - **Impact analysis** -- the impacted services and skills from 4a, what you
    checked and how, and any live service depending on a changed file that the
    apply does not already restart. The apply restarts the services agent for
    the system-interface backend, vendored-mngr source, `.mngr/settings.toml`,
    supervisord and bootstrap; anything beyond that is the lead's to carry
    out, and only your analysis can name it.
  - **Dockerfile split** (if it merged) -- each hunk as live-applicable (e.g. a
    `CLAUDE_CODE_VERSION` bump) or image-level (needs a manual rebuild).
  - **Provisioning changes** (if any `provisioner`-class file changed) -- per the
    impact analysis above, each change classified: **live-applicable** (name the
    in-branch edits you made to mirror it, e.g. an env var or `[agent_types]`
    change mirrored into the live env/pin -- the apply's own provisioner re-run
    and services restart then carry it) or **rebuild-only for the current
    workspace** (only a `build_arg` / `start_arg` / runtime-flag change a
    running container can't adopt, which the lead surfaces as a results-message
    caveat needing a workspace recreate). A genuinely-breaking, unapplyable
    change is a `stuck` report, not a `done`.
  - **Global-dependency bump with a dependent** (if the merge bumps a global dep
    that something depends on) -- the version delta and what your online research
    turned up, **which dependent(s)** and whether each is **built-in** (its code is
    in upstream at the target ref, so upstream-tested -> apply live) or
    **user-created** (absent from upstream, built in this workspace; couldn't
    validate -> rebuild-only, or `stuck` if it would break the running workspace).
    Judge by origin, not directory. Call out any gap honestly; the lead applies the
    built-in case and does not hot-apply the user case.
  - **Validation** -- suites/boots/Playwright run, all passing; **which branch
    of the 4c review-gate rule applied, with its evidence**: either the
    clean-pull skip (`has_merge_work: false` from classify-merge, an impact
    analysis with no user-created code in it, and no in-branch edits of your
    own) or the gate run's own record (the autofix fix commits kept vs
    reverted -- or "gate ran clean, no fixes proposed" -- and the
    architecture-gate verdicts). A report claiming the gates ran must carry
    that record; a report skipping them must show the rule's conditions
    held; a report with neither is incomplete and the lead will send it back.
    Also any validation **gap** (a coupled bump you couldn't fully exercise)
    called out honestly rather than implied as covered.
- `stuck` (`type: status`) -- you couldn't reach a clean, validated merge, or you
  hit the provisioning escape hatch above (a change you can neither apply live nor
  safely defer to a rebuild without breaking the running workspace); one sentence
  on what blocked you and where the work stands. Never report `done` on a merge
  whose suites or boots fail.

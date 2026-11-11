# Plan: bring the built-in skills in line with agent-skill best practices

The fix plan for the audit in `report-skills-best-practices-audit.md` beside this file.
The first release is given in file-level detail; later areas are outlined and get detailed when they start.

## Overview

- **Why size matters.** After a compaction Claude Code re-attaches only the first 5,000 tokens of each invoked skill (25,000 combined) and never re-reads the file. A reference that was merely read is not re-attached at all.
- **What 5,000 tokens is here.** Measured with `count_tokens` over all 111 markdown files under `.agents/`: tokens are bytes / 2.8 (5% spread). 5,000 tokens is 14 KB, about 250 lines of this prose. A 500-line skill is about 10,000 tokens.
- **Where we are.** 13 of 42 SKILL.md files are over 14 KB. `build-app` is 18,689 tokens and `update-self` 18,247.
- **One guide, two enforcers.** A skill authoring guide lives in mngr and is vendored into dwt. Deterministic rules go into `validate_skill.py`; judgment rules go into a `skill_hygiene` code-guardian category that defers to the guide.
- **Exemptions, not counts or warnings.** A skill that fails a check carries an exemption in its own frontmatter. The end state is that no built-in carries one. It is reached over several releases.
- **Shared references stay.** They exist so one fact has one home. The fix is tiering each large shared file into a core and rare-path siblings, never inlining.
- **`update-self` becomes self-contained for prose.** Its staged copy extracts the shared files it follows from the target release. No version floor is set now.
- **Users' existing skills are never restructured by a flow.** The update, migration and adoption stamp them and move on.
- **First release.** Foundations, the cheap cross-cutting PR, the staging change, `update-self`, `build-app` with `update-app`, and the lifecycle trio with the shared-layer tiering.
- **Deferred.** The template family, `migrate-workspace`, the medium skills, `imbue-studio` and the remaining scripts. Tracked in the Linear MIND backlog.
- **Decided against**, so nobody re-opens them by accident:
  - A size limit on reference files. The contents-list rule and the review category cover them.
  - A length cap on descriptions beyond the spec's 1,024 characters. Today's listing fits its budget.
  - A triggering check for the description rewrite. It adds no trigger vocabulary and triggering is not a problem today.
  - A numeric target for the load reports. The reviewer judges.
  - A separate terminology sweep. Each restructure applies the glossary to the files it touches.
  - An alias for the renamed browser command.
  - A version floor for `update-self` in this refactor.
  - A tree-wide old-patterns quarantine. Only `update-self` gets one; another skill gets one only when its shims keep it over a limit. Auditing every `CLEANUP:` marker is a separate Linear thread.
  - Merging `blueprint` and `blueprint-generate`. Two slash commands is the point of the pair.
  - `disable-model-invocation` on `harden-worker`. It would block the worker's own invocation.

## Expected behavior

### An agent following a built-in skill

- A restructured SKILL.md is at most 14 KB and opens with its standing instructions and step list.
- Each step names the reference it needs, so an agent that has been compacted knows what to re-read.
- A reference over 100 lines opens with a contents list. A SKILL.md over 200 lines opens with a step list or contents block naming its H2 sections.
- No file a skill reaches is more than two hops from its SKILL.md.
- Every file under a skill's directory, and every file under `.agents/shared/`, is reached from something.
- `dealing-with-the-unexpected` is gone; AGENTS.md points at `check-app-errors` and `assist`.
- The browser skill is `use-browser` and its command is `use-browser`. The old command name does not exist.
- A harden worker invokes the `harden-worker` skill. On a harness with no skill invocation it reads `.agents/skills/harden-worker/SKILL.md`.
- The catalog gains one short line for `harden-worker` and loses one for the deleted skill.

### An agent creating or editing a skill

- AGENTS.md tells it to run the validator when it edits a skill.
- A newly crystallized skill must pass every check with no exemption.
- An edit to a skill that carries an exemption must clear it.
- A failure states the goal and says restructuring is permitted. It does not offer the exemption as a way out.
- An exemption with a reason outside the closed set is rejected.
- The `skill_hygiene` category reviews the diff against the guide. For an exempt skill it skips the exempted criteria and judges only what the diff adds.
- The category applies in workspaces too, because dwt's categories file ships there.

### A user's existing skills

- The update, migration and adopting a template each stamp an exemption on every existing skill that fails a check.
- Nothing about the skill's content changes. The user is not told.
- Stamping recomputes: it adds an exemption where a skill fails and removes one that has gone stale.
- Skills listed in `.agents/third-party-skills.toml` are outside the checks, the stamping and the category. `imbue-studio` joins that list.

### The update flow

- After the hand-off, the staged skill reads shared prose from the target release, not from the workspace's older tree.
- The worker's impact analysis lists every name the update retires and searches the user's creations for each.
- A hit with a stated replacement, such as a renamed command, is fixed in the worker's branch. Any other hit is reported.
- Adoption runs the same search over the incoming template.

### A developer on the template

- The root suite has one tree-wide test that runs the validator over every skill that is not third-party.
- A stale exemption on a built-in fails that test, so the PR that fixes a skill must remove its exemption.
- Each restructure PR states the skill's common-path load before and after. There is no numeric target.
- mngr gains the guide and the category. Its own skills are measured once and their fixes filed as a follow-up.

## Implementation plan

Paths are in dwt unless marked (mngr).

### PR #1070: this plan and the corrected report

- `docs/system/blueprint/skills-best-practices/plan-skills-best-practices.md`: this file.
- `report-skills-best-practices-audit.md`: replace the estimated token figures with the measured ones, change "5 skills over budget" to 13 over 5,000 tokens, and remove the `welcome` finding (section 3.8 and Appendix C). That reference names the adopted repo's skill and is correct.
- `system/changelog/dwt-audit-agent-skills-best-practices.md`: mention the plan and the corrections.

### Foundations (one PR, paired mngr and dwt branches)

**The guide and the category**

- (mngr) `skill_authoring_guide.md` at the repo root. Sections:
  - Why: the compaction rule and the measured bytes-per-token figure.
  - SKILL.md: 14 KB and 500 lines; standing instructions and step list first; each step names its reference.
  - References: depth of two, contents lists, reachability, no orphans.
  - Descriptions: what, when and when not; triggers first; no body content; no first or second person. The shape is what Anthropic, OpenAI and agentskills.io agree on, so it does not overfit to one harness.
  - Old patterns: version-bound text goes in one reference with `CLEANUP:` markers.
  - Paths: a path that is not in this tree is written with a placeholder root.
  - Exemptions: what they are, the two reasons, who may write one.
  - Glossary of vendor-neutral authoring terms.
- (mngr) `.reviewer/code-issue-categories.md`: add `## skill_hygiene`, deferring to the guide the way `comment_cruft` defers to its skill.
  - It covers markdown an agent reads under `.agents/` and `.claude/skills/`: SKILL.md files, their references, the shared references and the worker references.
  - It skips third-party skills, skips exempted criteria, and judges only what the diff adds.
  - It judges what no script can: a SKILL.md written as the full manual, instructions or file paths in a description, first or second person, time-bound text outside an old-patterns reference, drift from the glossary, and a fixed procedure left as prose that should be a script.
- (mngr) `dev/changelog/<branch>.md`.
- `system/scripts/fetch_mngr_assets.sh`: add `skill_authoring_guide.md` to `ASSET_PATHS`.
- `docs/system/skill_authoring_guide.md`: symlink into `system/vendor/mngr-assets/`, like `style_guide.md`.
- `.reviewer/code-issue-categories.md`: the same category, pointing at the vendored guide.
- `.agents/shared/references/spec-summary.md`: shrink to dwt packaging rules plus the dwt glossary of product vocabulary; point at the guide for everything else. `user-facing-language.md` stays the authority for words shown to users.

**The validator** (`.agents/shared/scripts/agents_shared/validate_skill.py`)

- A closed set of check names: size, reachability, depth, paths, contents.
- A closed set of exemption reasons: one for skills that predate the checks, one for built-ins awaiting their restructure.
- Size: the SKILL.md file is at most 14,000 bytes. The 500-line body check stays.
- Reference graph: crawl links from SKILL.md through the skill's own files and `.agents/shared/`.
  - Reachability: every file in the skill directory is reached.
  - Depth: the shortest path to any reached file is at most two.
  - A reference chosen by filename pattern counts only where the concrete files are listed.
- Paths: every path named under a tracked prefix (`.agents/`, `system/`, `docs/`) exists. Paths under `data/`, absolute paths, globs and paths with a placeholder are skipped.
- Contents: the file opens with a list whose entries match its H2 headings, at the two thresholds above.
- Exemptions: read from frontmatter `metadata`, as one string naming the exempted checks and one naming the reason. An unknown check, an unknown reason or a stale exemption is an error.
- A mode that refuses any exemption, for crystallize and for an edited skill.
- A stamping mode that recomputes exemptions for every non-third-party skill in a tree and prints what it changed.
- A shared-layer function that lists files under `.agents/shared/` no skill and no AGENTS.md reaches.
- Failure text states the goal, permits restructuring and names the guide.

**The tree-wide test and the first stamping**

- `system/test_skills.py` (new): run the validator over every non-third-party skill; fail on any unreached shared file.
- Stamp each built-in that fails today with the awaiting-restructure reason. Size alone covers 12 skills; the other checks are measured when the code exists.
- `.agents/third-party-skills.toml`: list `imbue-studio` with its mngr source path.
- Add contents lists to long files that no first-release area rewrites. The rest keep an exemption until their area lands.
- `AGENTS.md`: one line telling agents to run the validator when they edit a skill.

**The flows that stamp**

- `.agents/shared/worker/references/op-crystallize.md` and `type-skill.md`: validate in the no-exemption mode; an edit clears an existing exemption.
- `.agents/skills/update-self/references/update-self-worker.md`: after the trial merge, run the stamping mode from the merged tree and list its output in the report.
- `.agents/skills/migrate-workspace/SKILL.md`: replace the hard validation of migrated skills with the stamping mode.
- `.agents/skills/use-template/SKILL.md` and `update-installed-template/SKILL.md`: run the stamping mode after the merge lands.
- These are minimal edits. The three skills' own restructures are deferred.

**Also in this PR**

- Changelog entries under `.agents/changelog/`, `system/changelog/` and `dev/changelog/` as touched.
- The PR body records one run of the size, reachability and contents checks over mngr's `.claude/skills/`. Size alone: 8 of 54 over 14 KB, none over 500 lines. File the follow-up ticket.

### The cheap PR

- **Descriptions.** All 41 to the house shape. Minimal wording edits, no new trigger vocabulary. `file-sharing`'s false promise is corrected.
- **Deletions.**
  - `.agents/skills/dealing-with-the-unexpected/`; AGENTS.md's section of that name points at `check-app-errors` and `assist`.
  - `.agents/shared/worker/references/verification.md` and the stale review-gates line in `lead-proxy.md`.
  - The Co-Authored-By line and `CLAUDE.md` wording in `submit-upstream-changes`; the dead pointer in `launch-task`'s dead-worker reference; the stale `skill-lifecycle-shared-refs` spec.
  - `use-template`'s reference to a v1 template's `welcome` skill stays.
- **Small content fixes.** For each skill that no area restructures, fix the report's findings that no check will catch. Examples:
  - `notify-user`: its `compatibility` line says the root venv while its command and script use plain `python3`.
  - `do-something-new`: its bail-out line names a skill to the user, against AGENTS.md.
  - `connect-external-service`: the secret-card procedure is stated three times across SKILL.md, `direct-api.md` and `mcp.md`; keep one. `latchkey.md` hard-codes a list of services that `latchkey services list` already gives.
- **`harden-worker` moves** from `.agents/shared/worker/` to `.agents/skills/harden-worker/`, with its `references/`.
  - Its description becomes one line saying it is not for a chat to invoke.
  - `heal-creation`, `update-creation`, `crystallize-creation`: the task file says to invoke the skill, with the path as the fallback.
  - `system/libs/app_manifest/src/app_manifest/scope.py`, its tests and README: the `type-*.md` convention paths.
  - `.mngr/settings.toml` comment and `system/test_mngr_template_stacking.py`: the worker is a tracked skill, still never an installed copy.
  - Every other mention: `update-self`, `build-app`, `migrate-workspace`, `AGENTS.md`, `docs/system/workspace-internals.md`, `system/services/host_backup/README.md`.
- **Constraint.** This PR and the staging change must ship in the same release. Between them, the staged `update-self` names worker files an older tree does not have.

### The staging change (built in this workspace)

- `.agents/skills/update-self/python/update_self_skill/update_self.py`: a new subcommand the staged copy runs after the hand-off. It extracts, from the target release into the staging directory:
  - `.agents/shared/`
  - `.agents/skills/harden-worker/`
  - the reference files of `launch-task` that the flow names
- `update-self/SKILL.md` and its references: every prose path outside the skill's own directory points at the staged copy.
- A test that every such path in the skill's markdown is in the extracted set and uses the staged prefix.
- Scripts keep today's rules: the four mirrored scripts and their sync test are unchanged.

### Area: `update-self` (parallel agent)

- **SKILL.md** becomes the lead's checklist, at most 14 KB. The detail of launching the worker, the question gates, the apply, the report-driven remainder and teardown moves to references.
- **`references/older-workspaces.md`** (new): every compatibility shim, each with a `CLEANUP:` marker keyed to 0.10.0 reaching stable.
  - Shims tied to 0.4.3, 0.6.1 and 0.8.1 workspaces.
  - Shims tied to launchers older than 0.10.0, including the three copies of the consume-the-report block.
  - The history bridge and the shallow-bake heal stay in the main path as one-line calls.
- **Worker guide.** `update-self-worker.md` becomes a short core plus rare-path files, with a contents list. The core tells the worker to re-read it after a compaction. `worker-provisioning-changes.md` gets a link from SKILL.md.
- **Duplicates removed**: the ceiling handling, the apply exit codes, the licence to stop apps and chats.
- **Impact analysis, generalised** (worker guide section 4a):
  - The rule: enumerate every name the update removes or renames (commands, flags, environment variables, ports, routes, skill names) and search the user's creations for each.
  - `update_self.py`: a subcommand that lists removed script entry points across every `pyproject.toml` and removed or renamed skill directories, between the base and the target.
  - The template's own changelog entries between the two releases are read the way mngr's are today.
- **The rename**, as its own PR stacked on this area:
  - `.agents/skills/agentic-browser-fleet/` becomes `.agents/skills/use-browser/`.
  - `system/apps/screen/pyproject.toml`: the entry point is renamed. No alias.
  - `system/apps/chat`: `message-kinds.ts`, `message_display.py` and their tests recognise the new name and keep recognising the old one in old transcripts.
  - Other callers: `message_chat.py`, `connect-external-service`, `use-screen`, `publish-template`'s readme recipe, the Screen specs, `system/apps/README.md`.
  - (mngr) the `minds_admin` demo skill that calls the command.
  - The changelog entry names the retired command, which is what the generalised rule reads.
- The dwt glossary is applied to every file the area touches. Exemptions on `update-self` are removed.

### Area: `build-app` and `update-app` (parallel agent)

- **`build-app`**
  - New references for the scaffold's output, wrapping an existing server, and the `forward_port.py` CLI.
  - Step 4 shrinks to its two commands and a pointer to `manage-desktop`.
  - One statement each for bind host, port picking, naming rules, the verify recipe, the public URL and the icon rules.
  - One step numbering, a checklist at the top, branches beside the step they fork from.
  - The eager read of `interactive-delivery.md` stays.
- **`update-app`**
  - New `references/verify-against-a-copy.md` for the data-copy guide.
  - Critical-app routing moves into a short top section, so the ordinary flow is not loaded for a critical change.
  - `critical-app.md` gets a contents list.
  - The loop is renumbered so the order matches the "verify before the user sees it" rule.
  - Facts shared with `build-app` get one owner and a link.
- The dwt glossary is applied to both. Exemptions are removed.

### Area: lifecycle trio and the shared layer (parallel agent)

- `.agents/shared/references/harden-lead.md` (new): the dispatch steps the three skills share. The single-flight check, the ticket, the task-file frontmatter, the launch, the go-live list.
- `heal-creation`, `update-creation`, `crystallize-creation`: each keeps its operation, its gate list and its substitutions.
- `lead-proxy.md`, `harden-contention.md` and `harden-worker/references/harden-creation.md`: each splits into a core that keeps its path and rare-path siblings the core links conditionally.
- `harden-worker/SKILL.md`: its load-when table lists the concrete operation and type files.
- `interactive-delivery.md` gets a contents list and a tighter opening.
- The dwt glossary is applied to every file the area touches. Exemptions on the three skills and on `harden-worker` are removed.

### Deferred areas (outline)

- **Template family.**
  - Port `build_template.sh` to the `publish_template_skill` package as a behaviour-preserving PR, held by its existing tests and exit codes. The update mode is added there afterwards.
  - Publish crawl: the build follows a published skill's shared references. Each must be a built-in of the base or included in the publish; otherwise the build refuses. The list and the publisher's dwt commit go into `template.toml`.
  - Adoption: `use-template` shows the agent the diff of each listed file between that commit and the adopter's tree, and runs the retired-names search over the incoming template.
  - The merge-and-boot-check shared by `use-template` and `update-installed-template`, and the installed-template ledger append, become scripts.
  - The five skills are restructured, which brings `publish-template` and `use-template` under 14 KB.
  - The latchkey permission-request body repeated in `publish-template`, `update-published-template` and `use-template` becomes one pointer at `connect-external-service`'s `latchkey.md`.
- **`migrate-workspace`.** Split the worker half out; restructure to 14 KB; replace its copy of the latchkey permission-request body with the same pointer.
- **Medium skills.** `manage-desktop`, `caretaker`, `file-sharing`, `github-sync`, `data-pipeline-builder`, `datalib`, and `use-browser`'s own restructure.
- **`imbue-studio`.** Restructured at its source in mngr.
- **Remaining scripts.** `submit-upstream-changes`' cherry-pick flow, `check-app-errors`' scan, `manage-routines`' timezone repair. Each scripted procedure, here and in the template family, is its own PR.
- Each area's acceptance: its skills pass every check with no exemption, the load report is in the PR, and its verification below has run.

## Implementation phases

1. **PR #1070.** Add this plan, correct the report. Merges first; everything else builds on it.
2. **Foundations.** One PR. After it, the tree-wide test is green, every failing built-in is stamped, and the guide and category exist in both repos.
3. **The cheap PR.** Built here, on the foundations.
4. **The staging change.** Built here, on the cheap PR. After it, the three areas are independent of each other.
5. **The three first-release areas, in parallel.** Each agent works in its own dwt worktree checked out in this workspace, branching from phase 4. Each stops at a pushed PR with green CI.
6. **Review and verification.** The user looks at each PR. Then the manual exercises and the eval run happen, possibly in parallel.
7. **The rename PR.** Stacked on the `update-self` area, merged with it.
8. **First release.** Phases 2 to 7 ship together.
9. **Deferred areas.** One at a time or in parallel, each removing its own exemptions, in later releases.

- Merge order follows the repo rule: the template PR first, then its paired mngr PR.
- No rebasing. A branch that falls behind merges its parent.

## Testing strategy

**Unit tests for the validator** (fixture skill directories built in a temp path)

- Size: a file just under and just over the cap.
- Reachability: an unlinked file in the skill directory fails; a linked one passes.
- Depth: a three-hop chain fails; the same file linked from SKILL.md passes.
- Paths: a missing tracked path fails; a `data/` path, a placeholder path and a glob are skipped.
- Contents: a long reference with no list fails; a list that omits a heading fails.
- Exemptions: a valid one suppresses its check only; a stale one, an unknown check and an unknown reason each fail.
- The no-exemption mode fails a skill that carries a valid exemption.
- Stamping: a failing skill gains an exemption, a fixed skill loses its stale one, a third-party skill is untouched.
- Shared layer: an unreached file under `.agents/shared/` is reported.

**Tests for the update flow**

- The staging subcommand extracts the expected paths from a fixture repository at a given ref.
- Every out-of-directory prose path in `update-self` is in the extracted set.
- The retired-names subcommand finds a removed entry point and a renamed skill directory in a fixture repository.

**Existing suites**

- dwt CI on every PR. `app_manifest`'s scope tests and the template-stacking test are updated in the cheap PR.
- mngr CI on the paired PRs.

**Behavioural verification, on demand**

- minds-evals: the existing `build-app` family at dwt main and at the branch, default Claude arm, comparing outcome and its skill gates. Covers `build-app` and `update-app`.
- A real `update-self` on a dev workspace from the current stable (0.9.5) to the first-release branch. Seed it with one user skill that fails a check and one that calls `agentic-browser-fleet`. Expect the first stamped and the second rewritten.
- Crystallize, update and heal one small skill on that workspace. Expect the worker to invoke `harden-worker` and the new skill to pass with no exemption.
- Deferred with the template family: publish and update against a throwaway repository, then adopt that template in a second workspace.
- `migrate-workspace` gets no behavioural check.

## Open questions

- **How Claude Code counts its 5,000 tokens.** The docs do not say. On the older tokenizer the same budget is about 18.7 KB. The 14 KB cap assumes the current one.
- **The skill listing budget.** Inferred as about 30K characters on a 1M-context model from one session's truncated listing. A workspace chat's listing is 22.9 to 24.9K today, so headroom is about 5K.
- **A path in another repository.** The placeholder-root convention is proposed here, not yet agreed. `use-template`'s `welcome` reference is the case that needs it.
- **The contents check on SKILL.md.** Requiring entries to match H2 headings exactly may reject a good checklist such as `publish-template`'s. Decide when the check is written against the real files.
- **How many built-ins fail the non-size checks.** Unknown until the validator exists. It sets how many exemptions the foundations PR stamps.
- **`imbue-studio` as third-party.** `test_python_launch_rules.py` and `test_skill_mngr_references.py` name it. Confirm nothing relies on it being checked.
- **A harden worker that catches up mid-pass.** A worker launched before the update follows its own worktree's copy, which is safe. One that merges the new HEAD mid-pass loses the path its task file names.
- **Retired names for templates published before the publish crawl.** They record no dwt commit, so adoption has no base to diff from.
- **The version floor.** Deferred until 0.10.0 is stable. The choice then is a two-hop update or staging the target's launcher.
- **`migrate-workspace` has no behavioural check.** Accepted for now.

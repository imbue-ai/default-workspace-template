# Report: the built-in skills against agent-skill best practices

Status as of 2026-11-04, `main` at `949248b6a`. An audit only: nothing in `.agents/` was changed. It catalogs how the 41 built-in skills under `.agents/skills/` and the shared layer under `.agents/shared/` measure up against the published guidance for authoring agent skills, so that a follow-up plan can fix them in priority order.

Method: the four source sets below were read in full. Every `SKILL.md`, every file under each skill's `references/` and `scripts/`, and every markdown file under `.agents/shared/` was then read in full (Python packages were skipped), with mechanical checks over the whole tree (sizes, frontmatter, link graph, duplication, time-bound phrasing) and per-batch close reading. The per-skill catalogs in the appendices were produced by seven parallel readers working from the same rulebook; the headline claims in sections 2 to 4 were re-verified by hand against the tree.

## 1. The rulebook

Sources: the Anthropic best-practices page (platform.claude.com, "Agent Skills best practices"), the Claude Academy course lesson "Configuration and multi-file skills", the full agentskills.io site (specification, quickstart, best practices, optimizing descriptions, evaluating skills, using scripts, client integration), the Claude Code skills documentation, the `anthropics/skills` repo (template, `skill-creator`, `quick_validate.py`), the `obra/superpowers` writing-skills guide, the nyosegawa synthesis, and two 2026 arXiv studies of SKILL.md corpora (2607.01456, 2608.08453). The repo's own `.agents/shared/references/spec-summary.md`, `type-skill.md` and `validate_skill.py` restate most of the same rules.

What the sources agree on, with the thresholds verbatim:

| Rule | Threshold | Sources |
|---|---|---|
| SKILL.md body length | "under 500 lines"; agentskills.io adds "< 5000 tokens recommended" | Anthropic, Academy, agentskills.io, Claude Code docs, skill-creator, `validate_skill.py` |
| SKILL.md is an overview | "like a table of contents": overview, quick start, core workflow; advanced features, API reference, long examples, long workflows go to separate files | Anthropic, Academy, agentskills.io |
| Reference depth | "Keep references one level deep from SKILL.md. All reference files should link directly from SKILL.md" | Anthropic, agentskills.io, skill-lint |
| Reference navigability | "For reference files longer than 100 lines, include a table of contents" (skill-creator says 300) | Anthropic, skill-creator |
| Link with a condition | each link says when to read the file ("Read X if the API returns a non-200 status code") | Anthropic, Academy, agentskills.io |
| Scripts | tell the agent to run them, not read them; bundle a script when the agent keeps reinventing the same logic; no interactive prompts | Anthropic, Academy, agentskills.io, skill-creator |
| `name` | 1-64 chars, lowercase letters, digits, single hyphens, must match the directory; consistent pattern across a collection (gerund suggested); no vague names (`helper`, `documents`, `data`) | agentskills.io, Anthropic |
| `description` | 1-1024 chars; third person; states what the skill does and when to use it, with the keywords users say; "100-200 words" at most; all "when to use" info goes here, not in the body | Anthropic, agentskills.io, skill-creator, Academy |
| Catalog cost | name + description of every skill is in context at all times, "about 50-100 tokens" each; Claude Code truncates a listing entry at 1,536 chars and budgets the whole listing at 1% of the context window | agentskills.io, Claude Code docs |
| Compaction | Claude Code keeps "the first 5,000 tokens" of each invoked skill after compaction, 25,000 combined, and does not re-read the file on later turns, so critical instructions go at the top | Claude Code docs |
| Content | no time-sensitive information (or quarantine it in an "old patterns" section); one term per concept; one default plus an escape hatch rather than a menu; no unexplained constants; only add what the model lacks | Anthropic, agentskills.io |
| Workflow shape | numbered steps, a copyable checklist for multi-step flows, validation loops, conditional branches signposted | Anthropic, agentskills.io |
| Self-containment | a skill is one directory; resources resolve relative to the skill root; nothing in the spec provides for files outside it | agentskills.io |

## 2. The state today, in numbers

| Measure | Value |
|---|---|
| Skills | 41 (40 built here, `imbue-studio` vendored from the mngr repo) |
| SKILL.md lines in total | 9,671 |
| SKILL.md bodies over 500 lines | 5 (`update-self` 971, `build-app` 868, `update-published-template` 552, `migrate-workspace` 543, `update-app` 534) |
| SKILL.md bodies over ~5,000 tokens | the same 5 (`update-self` ~10,000, `build-app` ~9,500, `update-published-template` ~6,200, `update-app` ~6,200, `migrate-workspace` ~5,300) |
| Skills with no `references/` at all | 25 of 41, including eleven over 150 lines (`manage-desktop` 327, `agentic-browser-fleet` 303, `update-creation` 282, `caretaker` 274, `fetch-process-show` 254, `update-installed-template` 252, `github-sync` 238, `data-pipeline-builder` 237, `imbue-studio` 217, `datalib` 216, `heal-creation` 203) |
| Reference files over 100 lines | 28 (13 in skills, 15 in shared); 1 has a table of contents |
| Reference files that are themselves over the 500-line SKILL.md budget | 2 (`update-self/references/update-self-worker.md` 649, `.agents/shared/worker/references/harden-creation.md` 552) |
| Orphaned reference files (no inbound link) | 3 (`blueprint-generate/references/plan-questions.md`, `update-self/references/worker-provisioning-changes.md`, `.agents/shared/worker/references/verification.md`) |
| Scripts a skill ships but never names in its SKILL.md | 4 in `publish-template` (`build_template.sh`, `scan_secrets.sh`, `write_template_manifest.py`, `betterleaks.toml`) |
| Longest reference chain from a SKILL.md | 8 hops (`heal-creation` to `worker/SKILL.md` to `op-heal.md` to `type-app.md` to `op-update.md` to `web-frontend-testing.md` to `harden-creation.md` to `harden-contention.md` to `lead-proxy.md`) |
| Cycles in the reference graph | at least 2 (`harden-creation` to `harden-contention` to `lead-proxy` to `worker-reporting` to `harden-creation`; `type-app` to `op-update` to `web-frontend-testing` to `type-app`) |
| Descriptions in second person | 16 of 41 |
| Descriptions with no when-clause | 4 (`blueprint`, `devices`, `notify-user`, `submit-upstream-changes`) |
| Descriptions over 500 chars | 7 (`update-app` 833, `caretaker` 717, `make-theme-icon` 566, `do-something-new` 552, `migrate-workspace` 551, `fetch-process-show` 526, `create-theme` 473 is just under) |
| Catalog cost, all 41 descriptions | 14,871 chars, about 3,700 tokens, in every chat's context |
| Byte-identical files in two skills | 2 (`blueprint` and `blueprint-generate` share `references/refine-prompt.md` and `references/templates.json`) |
| Verbatim lines (40+ chars) shared between sibling SKILL.md files | `use-template` and `update-installed-template` 32; `heal-creation` and `update-creation` 24 |
| Skills that depend on files outside their own directory | 19 of 41 (plus 30 skill reference files) |
| Enforcement | `validate_skill.py` encodes the 500-line limit and rejects the 5 oversize skills, but no test or CI job runs it against the built-in skills; it only runs when a worker crystallizes a new one |

How we got here, measured at release tags:

| Tag | Date | update-self | build-app | update-app | migrate-workspace | update-published-template | publish-template | skills | over 500 |
|---|---|---|---|---|---|---|---|---|---|
| minds-v0.5.0 | 2026-09-02 | 508 | 571 | 342 | 488 | 481 | 1,385 | 36 | 3 |
| minds-v0.7.0 | 2026-09-22 | 545 | 676 | 445 | 496 | 526 | 1,453 | 37 | 4 |
| minds-v0.9.0 | 2026-10-23 | 911 | 840 | 527 | 526 | 555 | 413 | 37 | 5 |
| minds-v0.10.0 | 2026-11-02 | 977 | 875 | 540 | 550 | 558 | 417 | 41 | 5 |

The one skill that shrank, `publish-template`, did so in commit `8e327598e` ("Restructure publish-template for progressive disclosure", 2026-10-20): the lead's checklist stayed in SKILL.md, the worker's instructions moved to `references/assembly-worker.md`, and every failure moved to `references/failures.md`. That commit is the in-repo precedent for the fix. Everything else has only grown.

### Effective context per activation

The 500-line rule is about what gets loaded when a skill triggers. Several skills say "read X first" or "follow X" unconditionally, so the real load is larger than the SKILL.md:

| Flow | Unconditional reads | Lines | ~Tokens |
|---|---|---|---|
| `update-self`, lead, ordinary pass | SKILL.md + `version-ceiling`, `handoff-contract`, `lead-proxy`, `apply-outcomes`, `results-message`, `freeing-memory-for-an-update` | ~2,090 | ~25,000 |
| `update-self`, worker | `update-self-worker` + `worker-reporting`, `worker-review-gates`, `worker-provisioning-changes`, `harden-creation` | ~1,640 | ~19,000 |
| `build-app`, canonical path | SKILL.md + `interactive-delivery.md` ("read first") + `frontend-choices.md` | ~1,045 | ~12,000 |
| `update-app`, critical app | SKILL.md (already loaded before the routing line says "stop reading here") + `critical-app.md` + `interactive-delivery.md` + `harden-contention.md` | ~1,580 | ~18,000 |
| `migrate-workspace`, worker | told to follow "Step 7" of the 550-line lead SKILL.md + `agent-recreation` + `pre-declutter-layout` + `worker-reporting` | ~1,240 | ~13,000 |
| `heal-creation` / `update-creation` / `crystallize-creation`, lead | SKILL.md + `harden-contention` (twice) + `lead-proxy` + `worker-reporting` | ~1,250 to 1,330 | ~13,000 |
| harden worker, crystallize a skill | `worker/SKILL.md` + `harden-creation` + `op-crystallize` + `type-skill` + `worker-reporting` + `transcript-exploration` + `spec-summary` + `skill-outline-fields` | ~1,515 | ~17,000 (about 2,900 lines with the named follow-ons) |
| `launch-task`, lead | SKILL.md + `lead-proxy` ("follow") | ~615 | ~8,000 |

Two of these matter beyond token cost. Claude Code keeps only the first 5,000 tokens of an invoked skill across compaction, so for `update-self` and `build-app` roughly half the SKILL.md, including the teardown and finalize steps at the bottom, disappears mid-task. And none of the files over 100 lines has a table of contents, so an agent that wants one section of `harden-contention.md` or `critical-app.md` has no way to find it short of reading the whole file.

## 3. The offenses, by kind

Each item names the rule, the offenders, and where the evidence is. Line-level citations are in the appendices.

### 3.1 Oversize SKILL.md (high)

Five skills break the 500-line and 5,000-token limits and fail the repo's own validator: `update-self`, `build-app`, `update-published-template`, `migrate-workspace`, `update-app`. A sixth, `update-self/references/update-self-worker.md`, is a second over-limit SKILL.md by any other name: a 649-line guide the worker is told to follow "end to end".

### 3.2 SKILL.md as the full manual rather than an overview (high)

The pattern the sources ask for is a short navigation layer plus the core workflow, with everything else behind a conditional link. What the tree has instead:

- `update-self`: about 650 of 970 body lines are step mechanics (a 132-line launch section with a 36-line heredoc task file, a 204-line apply section, a 96-line teardown). Appendix A tables them.
- `build-app`: about 450 of 868 lines are reference material: a file-by-file walkthrough of what the scaffolder generated (87 lines), a 94-line `forward_port.py` flag reference with a 44-line icon style guide embedded in one bullet (which already lives in `docs/system/app-icons.md`), an 88-line escape hatch with three near-identical supervisord program blocks.
- `update-app`: a 150-line "protect the user's data while you verify" guide (28% of the body) inline; `critical-app.md` is 492 lines with no table of contents.
- `update-published-template`: the entire 10-step worker procedure (about 100 lines) inlined into the lead's file rather than a worker reference as `publish-template` does; 46 lines of all-caps callouts before the first step (74 all-caps emphasis words in the body versus 4 to 17 in siblings).
- `migrate-workspace`: lead and worker halves share one file, so the worker is sent to "Step 7" and loads 430 lines it never uses.
- The eleven no-`references/` skills over 150 lines keep every rare path inline: `manage-desktop` (about 130 lines of lookup tables: popped-out windows, shortcuts and wallpaper, state field dictionary), `agentic-browser-fleet` (about 100 lines of crash recovery, long-list and screenshot gotchas), `caretaker` (a verbatim welcome message and permissions template as prose), `github-sync` (a 26-line supervisord block also described in the library README), `data-pipeline-builder` (eleven rule-dense sections, zero code blocks, no worked example), `file-sharing` (59 lines of synced-folder semantics), `imbue-studio` (67 lines of route recipes and trust model), `datalib` (33 lines of Cloudflare and desktop-proxy troubleshooting).
- Eager "read X first" loads defeat the tiering where it does exist: `build-app`, `fetch-process-show` and `do-something-new` all demand `.agents/shared/references/interactive-delivery.md` (143 lines) up front; `create-theme` sends the agent to about 170 lines of a 364-line blueprint plan before any CSS; `make-theme-icon` says "read the guide in full"; `publish-template`'s worker reads `assembly-worker.md` plus `readme-recipe.md` plus `worker-reporting.md` (about 520 lines) before step 1.

### 3.3 Reference chains more than one level deep, and cycles (high)

The rule is that every reference links directly from SKILL.md and nothing links onward. In the tree, 10 of `update-self`'s 14 references link to two or more further files; `critical-app.md` sends the reader to seven files across four skills and the shared tree; `launch-task`'s two references link each other and three shared files; `blueprint-generate/references/refinement.md` is the only route to `plan-questions.md`. In the shared layer the chains run four to eight hops (section 2) and the lifecycle core (`harden-creation`, `harden-contention`, `lead-proxy`, `worker-reporting`) is a cycle. Three pointers aim at bold run-in labels rather than headings (`op-heal.md:54`, `type-app.md:83`, `harden-contention.md:338`), and the second worker tier is reached by a filename template (`op-<OPERATION>.md`, `type-<TYPE>.md`) rather than by a link, so three of those files have zero literal inbound links.

### 3.4 Long references with no table of contents (medium)

28 reference files exceed 100 lines; only `publish-template/references/assembly-worker.md` has a table of contents. The largest without one: `update-self-worker.md` 649, `harden-creation.md` 552, `critical-app.md` 492, `lead-proxy.md` 409, `harden-contention.md` 405, `latchkey.md` 293, `apply-outcomes.md` 280, `pre-declutter-layout.md` 277, `spec-summary.md` 277, `worker-reporting.md` 236.

### 3.5 Orphans and unlinked scripts (medium)

`blueprint-generate/references/plan-questions.md`, `update-self/references/worker-provisioning-changes.md` and `.agents/shared/worker/references/verification.md` have no inbound link (the last lost its only pointer in commit `8c547bdbe` on 2026-09-10 and received five more edits on 2026-10-13 regardless). `publish-template` names only one of its five `scripts/` files in SKILL.md; `build_template.sh`, the primary script, is two hops away.

### 3.6 Duplication (high in the lifecycle and template families, medium elsewhere)

- `heal-creation` and `update-creation` are two copies of one flow: eight verbatim runs (single-flight check, ticket heredoc, task-file frontmatter, "commit, never stash", the Step 4 merge paragraph) and a go-live-by-type list written out a third time in `crystallize-creation`. The refactor that created `.agents/shared/` (commit `d66fba739`, 2026-04-29; design in `docs/system/specs/skill-lifecycle-shared-refs/concise.md`) factored the proxy mechanics and left the dispatch half behind.
- `use-template` and `update-installed-template`: a 45-line fetch, worktree-merge, boot-smoke-check and ff-only-land procedure copied verbatim, including a 16-line inline Python heredoc that a third skill describes in prose; about half of `update-installed-template` is `use-template` with "newer version" substituted.
- `blueprint` and `blueprint-generate`: two byte-identical reference files and a near-identical third, for one flow split in two so the user can type a second slash command.
- Within single skills: `update-self` states the exceeds-ceiling procedure, the Step 2 fault taxonomy, the apply exit codes and the stop-apps licence in SKILL.md and again in a reference; one `mkdir && mv` block appears three times in SKILL.md; `build-app` states the DNS-safe naming rule four times and the bind-host and port-picking guidance in both SKILL.md and `cross-flow-gotchas.md`; `caretaker` spells its three checks out twice; `agentic-browser-fleet` restates its ownership rules in three sections.
- Across the collection: the latchkey permission-request mechanics are written out in `imbue-studio`, `file-sharing`, `github-sync`, `publish-template`, `update-published-template`, `migrate-workspace`, `use-template` and `latchkey.md`; the "two unrelated v<n> axes" explanation in three template skills; `stop_when_no_windows` semantics and the OpenAPI route-table contract in both `build-app` and `update-app`; the Screen lease semantics in `use-screen`, `agentic-browser-fleet` and `manage-desktop`; the four-line worker report tail nine times across the three `op-*.md` files.

### 3.7 Descriptions (medium)

- Second person in 16 of 41 ("Use when you want to...", "whether or not you think a connection already exists", "YOU own the browser and YOU drive it").
- No when-clause: `blueprint` (86 chars, "Start a new plan-writing session. Explores the codebase and asks clarifying questions."), `devices` (78 chars), `submit-upstream-changes` (policy only), `notify-user` (prohibitions in place of triggers).
- Instructions or body content in the catalog: `do-something-new` tells the agent how to word its confirmation message; `update-app` (833 chars) spends more than half on what the skill owns and names a file path; `caretaker` (717 chars) is the whole weekly procedure; `create-theme`, `make-theme-icon`, `use-screen`, `use-from-phone`, `launch-custom-agent` and `imbue-studio` append a flow or feature summary after the trigger sentence.
- Inaccurate: `file-sharing` promises "importing Apple Notes, Photos" and the body has no procedure for either; `notify-user`'s `compatibility` says it runs from the root venv while the command runs it under the system `python3`.
- Missing the vocabulary users and the app actually use: `update-self` (no "update now", "new version", "upgrade"), `launch-task` (no "delegate", "background agent"), `use-template` (not the `/use-template <url>` line its own published READMEs print), `blueprint` (no "plan", "spec", "design doc" beyond the noun).

### 3.8 Time-bound, stale and dead content (high where dead, medium elsewhere)

Dead: `dealing-with-the-unexpected` tells the agent to inspect `data/.state/wait_counter` and "the wait script", which exist nowhere else in the tree; `use-template` twice defers to a `welcome` skill that does not exist in this template; `submit-upstream-changes` prescribes "the standard Co-Authored-By trailer used in this repo" (there is none, and it contradicts the repo owner's rule) and refers to "CLAUDE.md scaffolding" when the instructions file is `AGENTS.md`; `launch-task/references/dead-worker-recovery.md` sends finished workers to `submit-upstream-changes`, contradicting launch-task's own done-means-merge flow; `fetch-process-show` cites a principle "(CLAUDE.md)" that now lives in AGENTS.md.

Version-bound text interleaved with the live path rather than quarantined: `update-self` carries at least a dozen "an older workspace's launcher may not", "may predate", "the template's history was rewritten once" branches plus `minds-v0.3.6` and a one-time `stop_when_no_windows` back-fill; `migrate-workspace` opens with a changelog (`minds-v0.3.9`, Debian 12 to 13); legacy v1 manifest handling is spread through four template skills; `datalib` pins `v0.49.0` in prose three times; `find-transcripts` branches on the retired pre-ATIF transcript format inline; `manage-desktop` says "just `arcs` today", "no rename route yet" and lists the old tabbed-shell verbs; `use-from-phone` hard-codes iOS and Android menu labels; `latchkey.md` hard-codes about 27 service names and then says the real list comes from a command; the shared layer has "older launchers stamped", "a ticket opened before titles carried types", and measured millisecond constants justifying rules. None of it carries a `CLEANUP:` marker or an "old patterns" section.

### 3.9 Deterministic procedures left as prose (medium)

The repo's own `spec-summary.md` classifies a step that runs the same code every time as a `[script]` step. Left as prose anyway: `update-published-template`'s destructive 10-step reset-overlay-rescan-commit sequence (the body says so: `build_template.sh` has no `--update` mode); `update-self`'s previous-worker cleanup and catch-up messaging branches; `submit-upstream-changes`' fetch, worktree, cherry-pick, push sequence with the same 5-line `tomllib` snippet pasted three times; `manage-routines`' three-command timezone repair; `find-transcripts`' 15-line jq renderer; `check-app-errors`' log scan (which the Caretaker runs weekly); `assist`'s git-ancestry origin classification; `update-installed-template`'s 35-line hand-written ledger-append contract (the publisher side has `record_publish.py`); `use-ai-integration` tells consumers to copy a 495-line `claude_p.py` into each app instead of importing the package it already ships.

### 3.10 Terminology (medium)

The same concept under several names, within one file: the delegated agent is "worker", "sub-agent", "background agent", "sibling", "sub-worker" (259 / 3 / 3 / 21 / 21 uses in the shared layer alone); the thing `build-app` builds is "app", "service", "lib", "program", "site", "view", "window", and its phases are both "Step 0-5" and "skeleton phase 1-7"; `update-self` says "pass", "run", "flow" and "update", and "all seven" frontend bundles in one place and "all three" in another; `update-app`'s prototype is "hand mock", "Type 1", "Type 2", "preview"; the template family numbers steps as `§2e`, `step 9`, `Step 3` and `2b` across five files that cross-reference each other.

### 3.11 Naming (low to medium)

26 names are imperative verb-object, 11 are bare nouns (`devices`, `datalib`, `caretaker`, `assist`, `blueprint`, `imbue-studio`, `agentic-browser-fleet`, `file-sharing`, `github-sync`, `handoff-summary`, `data-pipeline-builder`), one is a gerund phrase, two are verb chains. Five `update-*` names denote four different objects. `agentic-browser-fleet` no longer describes a fleet (the body caps it at one Chromium). `.agents/shared/worker/SKILL.md` declares `name: harden-worker` in a directory called `worker`, which `validate_skill.py` would reject.

### 3.12 The shared layer is outside the spec's model (medium)

19 skills and 30 skill reference files depend on `.agents/shared/`. The spec's resource model is relative to the skill root, and the publishing path honors that for third-party skills (kept as they came) but not for built-ins: `publish-template`'s `build_template.sh` stages only `--include` paths over a clean base, `write_template_manifest.py` lints only secrets, and nothing checks that a published skill's `.agents/shared/...` pointers exist in the adopter's base at a compatible version. `assembly-worker.md` already lives with one such skew. The design spec that introduced the layer promised "one-level references" and "no SKILL.md in shared"; the tree has eight-hop chains and a SKILL.md there, and the spec itself names scripts, flows and skill names that no longer exist.

### 3.13 Enforcement gap (high)

`validate_skill.py` (`_MAX_BODY_LINES = 500`, name rules, description length) is the right check and already exists, but `system/` tests import only its helper functions. Nothing runs `validate()` over `.agents/skills/*`, so five built-in skills fail a check every crystallized user skill must pass. There is likewise no check for orphaned references, reference depth, descriptions' voice or length budget, or dead paths in prose (the `wait_counter` lines have survived since the skill was written).

## 4. Offenders, ranked

| Skill | Body lines | ~Tokens | Refs | Worst offenses |
|---|---|---|---|---|
| update-self | 971 | 10,000 | 14 (1,924 lines) | 2x the limit; full manual; 649-line worker guide; 3-deep chains; orphan ref; a dozen version-bound shims inline; duplicated procedures; 2nd-person description |
| build-app | 868 | 9,500 | 5 | ~450 lines of reference material inline (CLI reference, escape hatch, scaffold walkthrough); eager read-first; 7 facts stated 2-4 times; six names for one noun |
| update-app | 534 | 6,200 | 1 (492 lines, no TOC) | 150-line data-copy guide inline; 833-char 2nd-person description with a file path; critical-app.md chains to 7 files; step numbering contradicts itself |
| update-published-template | 552 | 6,200 | 2 | worker recipe inlined; destructive 10-step git sequence as prose; 46 lines of all-caps callouts; invariants stated three times |
| migrate-workspace | 543 | 5,300 | 2 (173, 277, no TOC) | lead and worker in one file; opens with a changelog; legacy layout handling inline; 1st-person quotes in description |
| heal-creation / update-creation / crystallize-creation | 197 / 276 / 255 | 1,800 / 2,700 / 2,300 | 0 / 0 / 2 | three copies of one flow; ~1,250-1,330 lines of shared refs per run; 2-level chains into 400-line files with no TOC |
| launch-task | 206 | 2,000 | 2 | core workflow lives in a 409-line shared file; refs chain 2-3 deep; stale pointer to submit-upstream-changes |
| use-template / update-installed-template | 356 / 246 | 3,800 / 2,650 | 0 / 0 | 32 verbatim lines shared; inline Python heredoc copied; v1 handling in five places; nonexistent `welcome` skill |
| manage-desktop | 320 | 4,800 | 0 | 40% lookup tables inline; "today"/"yet" statements; legacy verbs listed inline |
| agentic-browser-fleet | 297 | 3,800 | 0 | ~100 lines of conditional recovery inline; instruction-laden 2nd-person description; name no longer true |
| caretaker | 268 | 3,700 | 0 | 717-char description is the procedure; assets as prose; checks spelled out twice |
| connect-external-service | 71 | 1,450 | 3 (293, 184, 92) | good routing table, but two refs over 100 lines without TOC; secret-card procedure three times; 27 hard-coded service names |
| blueprint / blueprint-generate | 83 / 38 | 1,000 / 220 | 3 / 5 | identical duplicated refs; orphan + nested ref; no when-clause; non-ASCII progress glyphs |
| dealing-with-the-unexpected | 34 | 300 | 0 | dead `wait_counter` content; teaches nothing the model lacks; overlaps `assist` and `check-app-errors` |
| submit-upstream-changes | 112 | 960 | 1 | no when-clause; Co-Authored-By instruction; config-vs-hardcoded contradiction; snippet pasted three times |
| file-sharing | 149 | 2,000 | 0 | description promises Notes/Photos import the body lacks; workflow hidden in an "Examples" appendix |
| use-ai-integration | 229 | 2,200 | 1 | "copy this file" instead of import; 52 lines of credential setup before the decision; model ids and a dated cost table |
| data-pipeline-builder | 231 | 3,400 | 0 | rule prose with no example; nine unexplained constants; test layout contradicts `type-skill.md` |
| imbue-studio (vendored) | 211 | 2,900 | 0 | full manual inline; permission boilerplate duplicated; description is a table of contents (fix upstream in mngr) |
| `.agents/shared/` (27 files) | 3,973 total | ~34,000 | | 4-8 hop chains and cycles; 15 files over 100 lines with no TOC; orphan `verification.md`; `name` does not match directory; op-* scaffolding and report tail repeated nine times |

Skills with only low-severity findings: `notify-user`, `handoff-summary`, `show-files-in-chat`, `launch-custom-agent`, `manage-routines`, `devices` (thin and API-shaped, but short), `use-from-phone`, `check-app-errors`, `make-theme-icon`, `use-screen`, `assist`, `create-theme`, `find-transcripts`, `datalib`, `github-sync`, `fetch-process-show`, `publish-template`.

## 5. What is already right

- `publish-template` after `8e327598e`: a copyable ten-step checklist with the two "end turn, wait" gates marked, worker instructions in a reference with a table of contents, failures as an exit-code table in their own file. The shape every lead skill should have.
- `connect-external-service`: SKILL.md is a routing table whose last column names exactly which reference and section to read per row.
- `.agents/shared/worker/SKILL.md`: a "Load when / Reference / What it gives you" table with precise conditions, the pattern the sources recommend and most SKILL.md files lack.
- `crystallize-creation` and `migrate-workspace` quarantine their rare paths behind an explicit condition ("Load this only when `detect-layout` reported `pre-declutter`").
- Scripts are run, not read, nearly everywhere; deterministic work in `update-self`, `launch-task`, `publish-template` and `migrate-workspace` is already in subcommands with exit codes the prose interprets.
- `validate_skill.py`, `scaffold_skill.py` and `spec-summary.md` already encode the spec; the gap is that built-ins are not held to them.
- Every named path in the shared layer resolves, and every heading-level section pointer resolves.

## 6. Caveats

- Token figures are estimates (words x 1.35); line counts are exact.
- `imbue-studio` is vendored from the mngr repo by `system/scripts/vendor_mngr_skill.py` and CI checks it matches the pinned commit; its findings belong upstream.
- The appendices were written by seven parallel readers. The claims in sections 2 to 4 that carry the most weight (the oversize list and validator failures, the orphans, the dead `wait_counter` and `welcome` references, the Co-Authored-By line, the Notes/Photos description, the identical blueprint files, the verbatim-line counts, the worker SKILL.md name, the eager `interactive-delivery.md` reads) were re-checked by hand against the tree. Line numbers in the appendices were not individually re-verified.
- This report deliberately proposes no plan. The remedies named inline (move sections to references, add tables of contents, script the fixed sequences, run `validate_skill.py` over built-ins in CI) are the obvious ones; ordering and scope are for the follow-up.

## Appendices: per-skill catalogs

Each appendix is one reader's catalog for its batch, reproduced as written. Severity labels, source abbreviations and line citations are the reader's own.

### Appendix A: update-self

#### Audit: `update-self`

Paths are relative to `.agents/skills/update-self/`. Sources cited: **A** = Anthropic best-practices page, **C** = Academy course, **S** = agentskills.io spec / best-practices, **R** = repo's own `spec-summary.md` + `validate_skill.py` + `type-skill.md`.

##### Size

| Measure | Value | Limit | Source |
|---|---|---|---|
| SKILL.md body lines (after frontmatter) | 970 | 500 | A, C, S, R (`validate_skill.py` `_MAX_BODY_LINES`) |
| SKILL.md body words / est. tokens (x1.35) | 7,406 / ~10,000 | 5,000 tokens | S |
| Reference files | 14 files, 1,924 lines, ~19,200 words | | |
| Largest reference (`update-self-worker.md`) | 649 lines, 5,680 words (~7,700 tokens) | >100 lines wants a TOC (A); >500 lines is itself over the SKILL.md ceiling it is meant to relieve | A, S |
| Whole skill, prose only | 2,901 lines, ~26,600 words (~36k tokens) | | |

- **HIGH -- body is 194% of the 500-line limit and ~2x the 5,000-token guidance.** `validate_skill.py` (the repo's own validator, which the `type-skill.md` worker reference says must print `ok` for every skill) fails this skill statically: `SKILL.md body is 970 lines; spec recommends <= 500`. Remedy: move Step 5b/5c/6 mechanics and the Step 3b launch recipe into references.

##### Progressive disclosure

**HIGH -- SKILL.md is the full operator manual, not an overview + core workflow (A: "SKILL.md is a table of contents"; S: "concise, stepwise guidance with a working example outperforms exhaustive documentation").** Sections that are detail, with the line ranges:

| Heading | Lines | Approx. lines | Why it is reference material |
|---|---|---|---|
| Intro | 8-37 | 30 | Three paragraphs of rationale (why a worker, why the staging path) before the first step; the "why" belongs in `handoff-contract.md`, which already says it. |
| 1. Preconditions, "What this pass may do without asking" | 55-85 | 31 | A licence rule-set that `freeing-memory-for-an-update.md` also states (duplicated, see below). |
| 1. Preconditions, lease takeover rules | 87-114 | 28 | Contention procedure; `harden-contention.md` (405 lines) is already cited for it at line 101. |
| 2. Resolve the target, error taxonomy | 191-213 | 23 | Per-exit-code verdict table duplicating `version-ceiling.md` lines 10-19. |
| 2, exceeds-ceiling confirmation | 214-224 | 11 | Duplicates `version-ceiling.md` "Overrides past the ceiling" (lines 33-53) nearly sentence for sentence. |
| 3a. Re-check the ceiling | 264-316 | 53 | The *reason* for 3a is in `version-ceiling.md` lines 55-74; SKILL.md re-explains it (282-292), then adds the history-bridge rationale (298-316). |
| 3b. Launch | 318-449 | 132 | A 36-line heredoc task-file template (338-374), previous-worker cleanup with branch-archive logic (376-414), and a three-command launch. The task file text is a fixed artifact that should be a template in `assets/` or emitted by `update_self.py`. |
| 4. Proxy the `question` gate | 451-537 | 87 | Four gate kinds plus a four-step hold procedure; a classic "conditional details -> separate file" case (A, pattern 3). |
| 5a. Audit the report | 560-592 | 33 | One 20-line paragraph (562-581) listing every evidence item the worker report must carry -- a checklist that belongs beside the worker contract. |
| 5b. Apply the update | 594-797 | 204 | The single largest section: critical-app lease rules (609-633, one 25-line sentence), frontend rebuild rationale (635-646), catch-up protocol with exit-code semantics (651-705), chat pause/WIP-commit (707-742), the apply command and its exit codes (744-778, duplicating `apply-outcomes.md` 6-142), resume (780-797). |
| 5c. Report-driven remainder | 799-846 | 48 | Per-finding dispositions incl. a 20-line `stop_when_no_windows` rule (827-846) unrelated to applying an update. |
| 6. Teardown | 877-972 | 96 | Eight commands with their ordering rationale; the stray-preview cleanup (910-921) is a corner case. |

- Of 970 body lines, roughly 650 (Steps 3b, 4, 5a-5c, 6) are procedure detail that A/S say to push into references and link conditionally; the overview + core workflow (Steps 1-2, 2a, the skeleton of 3-6) would fit in ~250 lines.
- **MEDIUM -- no navigation layer.** There is no step list / table of contents at the top and no "when to read which reference" index until line 863 ("Outside the main flow"), after 850 lines. A: "SKILL.md ... points Claude to detailed materials"; S: "tell the agent when to load each file".

**Eager loads / effective context.** SKILL.md has no "read X in full first" instruction, which is good. But the body itself is ~10k tokens, and the first step already points at three more files as governing rules (`freeing-memory-for-an-update.md` 97, `results-message.md` 142, `harden-contention.md` 405), and Step 4 at `lead-proxy.md` (409). Effective load for a lead that follows the cited rules on an ordinary pass: SKILL.md 977 + `version-ceiling.md` 75 + `handoff-contract.md` 113 + `lead-proxy.md` 409 + `apply-outcomes.md` 280 + `results-message.md` 142 + `freeing-memory-for-an-update.md` 97 = **~2,090 lines (~25k tokens)** before any gate or failure path. A worker following `update-self-worker.md` loads 649 + `worker-reporting.md` 236 + `worker-review-gates.md` 98 + `worker-provisioning-changes.md` 108 + `harden-creation.md` 552 (cited twice) = **~1,640 lines**. Severity MEDIUM: the volume is partly inherent to the task, but the SKILL.md half is avoidable.

##### References

**HIGH -- reference chains are two and three levels deep (A: "keep references one level deep from SKILL.md"; S: "avoid deeply nested reference chains").** Concrete chains:

- `SKILL.md` -> `references/update-self-worker.md` -> `references/worker-review-gates.md` -> `.agents/shared/worker/references/harden-creation.md` (552 lines; "Review gates" and "The test gate" sections).
- `SKILL.md` -> `references/update-self-worker.md` -> `references/python-packaging-migration.md` -> `.agents/shared/references/running-python.md` (201) and `.agents/shared/references/spec-summary.md` (277).
- `SKILL.md` -> `references/update-self-worker.md` -> `references/worker-provisioning-changes.md` (this is the *only* route to that file; see orphan below).
- `SKILL.md` -> `references/apply-outcomes.md` -> `references/results-message.md` -> `references/resuming-after-a-decision.md` -> (back to) SKILL.md Steps 1, 3a, 3b, 5b.
- `SKILL.md` -> `references/handoff-contract.md` -> `references/version-ceiling.md` -> `references/handoff-contract.md` (mutual).
- `SKILL.md` -> `references/freeing-memory-for-an-update.md` -> `.agents/shared/references/freeing-memory.md` and `references/results-message.md`.
- `SKILL.md` -> `references/update-self-worker.md` -> `.agents/shared/references/worker-reporting.md`, `.agents/shared/scripts/agents_shared/serve_isolated_instance.py` (1,529-line script, pointed at "see `update-app`"), `.agents/shared/worker/references/web-frontend-testing.md`.

Every reference except `heal-shallow-history.md` and `resuming-after-a-restart.md` links onward to at least one other reference; 10 of 14 link to two or more.

**MEDIUM -- orphan reference (A: watch for "never-read files"; files must link directly from SKILL.md).** `references/worker-provisioning-changes.md` (108 lines) is never named in SKILL.md (confirmed: `grep worker-provisioning SKILL.md` returns nothing). It is reached only from `update-self-worker.md` lines 299 and 537. The lead's 5c "Rebuild-only flags" and "Dockerfile" bullets (816-826) restate its classification without pointing to it.

**MEDIUM -- long reference files with no table of contents (A: "for reference files longer than 100 lines, include a table of contents at the top").** None of the 14 has one. Over 100 lines: `update-self-worker.md` (649), `apply-outcomes.md` (280), `python-packaging-migration.md` (225; it has a symptom table at 41-51 that acts as one, the closest thing to compliance), `results-message.md` (142), `handoff-contract.md` (113), `worker-provisioning-changes.md` (108).

**MEDIUM -- `update-self-worker.md` is a second over-limit SKILL.md.** 649 lines / ~7,700 tokens, eight numbered sections, 15 code blocks, and its own sub-references. It is the worker's primary instruction set, handed over as a task file pointer (SKILL.md 353-355), so it is loaded whole, not progressively. Remedy: give the worker its own skill directory (as `.agents/shared/worker/SKILL.md` does for the harden worker) with references under it.

**MEDIUM -- duplicated text between SKILL.md and references, and between references.**

- Exceeds-ceiling handling: SKILL.md 214-224 vs `version-ceiling.md` 33-53 (same procedure, same quoted detail string for `REFUSED`).
- Step 2 fault taxonomy: SKILL.md 191-213 vs `version-ceiling.md` 10-19.
- Why 3a re-checks: SKILL.md 282-292 vs `version-ceiling.md` 55-74 vs `handoff-contract.md` 20-22.
- Licence to stop apps/chats: SKILL.md 55-85 vs `freeing-memory-for-an-update.md` 26-57 vs `results-message.md` 63-87 ("What the pass did on the user's behalf").
- Apply exit codes: SKILL.md 752-778 vs `apply-outcomes.md` 6-142 (SKILL.md says "each spelled out in apply-outcomes.md" and then spells them out).
- Catch-up sender block: SKILL.md 674-681 is repeated verbatim in `resuming-after-a-decision.md` 43-51 and by reference in `apply-outcomes.md` 64-72.
- Consume-the-report `mkdir -p ... && mv ...` block appears three times in SKILL.md (518-522, 669-673, 946-951).
- Re-take preconditions (lease, `run-status start`, `REF=$(sed ...)`, bridge) appears in `resuming-after-a-decision.md` 19-30 and `resuming-after-stuck.md` 7-18 nearly verbatim.
- The seven frontend bundle paths are listed in SKILL.md 752-759 and `update-self-worker.md` 510-518; §7 of the worker guide (622-626) still says "rebuild all three", contradicting the seven elsewhere (terminology/consistency defect).

**LOW -- file naming.** Names are descriptive (good). Two are misleadingly scoped: `handoff-contract.md` opens "Maintainer notes for editing the update-self skill" (lines 3-4), i.e. it is developer documentation, not runtime instruction, yet it sits in `references/` where the executing agent is told to read it (SKILL.md 258-260). `heal-shallow-history.md` is 26 lines whose content is already inlined as a comment in SKILL.md 164-170 and in the worker guide 29-33.

##### Description frontmatter

`description` (328 chars, 45 words): "Safely pull updates from the upstream template repo (default target is the release the running Imbue Studio app was built against). Use when you want to incorporate upstream skills, script fixes, or config improvements. For pushing local improvements back upstream, use the `submit-upstream-changes` skill instead."

- **MEDIUM -- second person.** "Use when *you* want to incorporate" (A: "always write in third person"; the page's explicit bad example is "You can use this to...").
- **MEDIUM -- trigger coverage is thin for how the skill is actually invoked.** The body says the pass is started by the Imbue Studio app's message, scheduled or pressed (lines 20-22, 147-151, `handoff-contract.md` 63-83), with "Update now" / "Update to a specific version" phrasing, and `/migrate-workspace` hand-offs; none of those terms (update now, workspace update, new version, upgrade, template release, `minds-v*`) appear in the description. User phrasings like "update my workspace", "is there a newer version", "upgrade to the latest" are not covered (C: "add keywords that match how you actually phrase requests").
- **LOW -- a parenthetical mechanism ("default target is the release the running Imbue Studio app was built against") and a cross-skill routing sentence occupy half the description.** Routing to `submit-upstream-changes` is useful; the mechanism is body material.
- Length is fine (328 of 1,024). Name matches directory; `metadata.author` is the only extra key. No `compatibility` even though the skill needs `tk`, `mngr`, `jq`, `uv`, latchkey and a system Python >= 3.12 (`scripts/update_self.py` 21-42) -- S says `compatibility` is for exactly "system packages, network access" requirements (LOW).

##### Content

**HIGH -- time-sensitive and version-bound text throughout (A: "avoid time-sensitive information", put legacy handling in an "Old patterns" section).** The whole SKILL.md is written to run "cross-version" against workspaces from older releases, and the compatibility shims are interleaved with the main path rather than quarantined:

- SKILL.md 164-170, 227-229, 298-316: `--depth 1` pool-bake heal and the "template's history was rewritten once" bridge -- one-time events, run on every pass.
- 282-292: "the only ceiling check that runs on a workspace updating into the ceiling for the first time (its local copy may predate the check)".
- 334-335, 367, 426, 484, 516, 663-666: "an older workspace's launcher may not stamp it", "which this workspace's own launcher may predate", "this workspace's own tree may predate the script", "an older own copy drops the record", "an older launcher's `await` leaves it in place", "may predate a reply subcommand, so send through ... where the workspace has it, and otherwise through `mngr message`".
- 837-846: `stop_when_no_windows` back-fill "for an app built before the field existed" -- a one-off data migration embedded in the update flow.
- 910-921: cleanup of a stray preview "an older pass may have left".
- `handoff-contract.md` 30, 59: "read by every release from minds-v0.3.10 on", "`message_chat.py` where there is one (minds-v0.6.1 on)"; 81-83: "The `--ceiling` instruction exists only for the Step 2 copies that predate the pin; the app drops it once...".
- `scripts/update_self.py` 5-6, 30: "a local Lima workspace from before 2026-09-14, on Debian 12"; "predates minds-v0.4.3".
- `heal-shallow-history.md` 3: "baked before the full-history bake fix".
- `python-packaging-migration.md` is entirely a one-release migration guide ("The release that moved every skill's Python into a package...").
- SKILL.md 181 cites `minds-v0.3.6` as an example override, an old release.

None of these carries a `CLEANUP:`-style marker saying when it can go (repo style guide asks for one on rollout-bridging code).

**MEDIUM -- over-explanation / rationale in the instruction path (A: "default assumption: Claude is already very smart"; "does this paragraph justify its token cost?").** Examples: 635-646 (why frontends must be rebuilt, 12 lines, then "the apply command below does this itself"); 393-414 (why `-D` not `-d`, why archive); 416-424 (why the poll is backgrounded); 917-921 (why `close` precedes `down`); 8-37 (intro). `handoff-contract.md` "Trust" (107-113) and "What the apply must tolerate" (85-105) are design notes.

**MEDIUM -- inconsistent terminology for the same things (A: "pick one term").**
- The delegated agent is "worker" (86x), "background agent" (4x, incl. the user-facing word), "sub-agent"/"repair worker" (5c step 2).
- The invocation is "pass" (56x), "run" (72x), "flow" (12x), "update" interchangeably ("this pass", "the run record", "the update flow", "the careful flow").
- Frontend bundles: "all seven" (SKILL.md 752-759; worker guide 412-414, 510-518) vs "all three" (worker guide 622) vs "the three" nowhere defined.
- Verdict spelled `run-status verdict X` in prose but also as bare `UPDATED`, `NEEDS_DECISION`; the hold is "a hold", "the §4b hold", "a customization hold", "a needs-decision ending".
- Lease names are quoted sometimes with backticks and sometimes not (`updating workspace`, "editing critical app <name>").

**MEDIUM -- multi-option branches without a stated default.** 5b catch-up messaging (660-681): `message_chat.py` if present and `WORKER_ID` set, else `mngr message`; then five exit-code outcomes (683-691) each with a different action. Step 3b branch cleanup (389-414): three outcomes of `merge-base --is-ancestor`. These are correct but are exactly the "narrow bridge" case A says to script (low freedom) rather than narrate; `update_self.py` already has `run-status`, `bootstrap-skill`, `bridge-history` subcommands, so "clear previous worker", "catch up worker", "consume report" and "take leases" are the obvious next scripting targets (S: "bundle scripts when the agent keeps reinventing the same logic").

**LOW -- "read, don't run" leakage.** Scripts are told to be run (good). But the task file (SKILL.md 353-355) tells the worker to "follow the worker guide ... end to end" (649 lines), and 5a tells the lead to audit against "the staged copy's `references/update-self-worker.md`, §4a, §4b, §4c and §6", so the lead reads the worker's manual too.

**LOW -- magic numbers without justification (A: "no voodoo constants").** `--timeout 90m` (431), exit codes 7/8 for the messenger (683-691) and 75 for the Python floor (278) are explained only partially; 90m is not explained.

**LOW -- positive: no Windows paths, no emoji, no README/CHANGELOG inside the skill, forward slashes throughout, `metadata` and `compatibility` usage otherwise conformant.**

##### Workflow structure

- **Good:** numbered top-level steps (1-6) with sub-steps (2a, 3a, 3b, 5a-5d); explicit exit-code handling after every command; every terminal path names its verdict; "Outside the main flow" (863-875) is a correct conditional-details index.
- **MEDIUM -- no checklist form (A: "provide a checklist Claude can copy and check off" for complex workflows).** With ~25 commands, five leases/records to open and close, and ordering constraints ("close goes first", "each `tk` call as its own command", "take them right before the apply"), nothing is presented as a checklist; the ordering rules are buried in prose.
- **MEDIUM -- branches are signposted by prose, not structure.** Step 4's four gate kinds (459-498), 5b's six "before the apply" conditions, and 6's "the rest is only for exit 0" (923) are discovered by reading; A's conditional-workflow pattern ("Creating? -> section X / Editing? -> section Y") is not used.
- **MEDIUM -- feedback loops exist but are described rather than run.** The audit (5a) is a human-style review of a prose report; A/S recommend a validator script ("plan-validate-execute") with specific error output. The report's required evidence list (562-581) is a natural schema for `update_self.py` to check.
- **LOW -- forward references the reader cannot resolve in order.** Step 1 says "5b, and earlier with `--agent <name>`" (line 61) and "Step 4" (line 24) before those sections exist; 5b says "run Step 6's opening commands" (526); Step 4 hold step 3 says the same; `apply-outcomes.md` repeatedly says "as SKILL.md 5b does". Readers jump both ways between files.

##### What the skill does well

- Every command checks its exit code and names what to do on each one; terminal states are enumerated (`apply-outcomes.md` "Which verdict ends the pass").
- Deterministic work is scripted (`update_self.py` with `resolve-target`, `bootstrap-skill`, `bridge-history`, `classify-merge`, `footprint-ranges`, `pending-rollbacks`, `apply`, `run-status *`) and the prose says "call it, don't reimplement" (worker guide 9-12).
- Rare paths (`resuming-after-*`, `stopping-when-asked`) are correctly split into small, conditionally loaded files.
- Reference file names are descriptive, not `doc2.md`.
- User-facing language rules are centralized (`results-message.md`), including the plain-language vs. verbatim-technical distinction for bug reports.
- `validate_skill.py` exists and encodes the 500-line rule; the problem is that this skill is not held to it.

##### Summary of offenses by severity

| Sev | Offense |
|---|---|
| HIGH | Body 970 lines / ~10k tokens; fails the repo's own validator |
| HIGH | SKILL.md is the full manual; ~650 lines of Step 3b/4/5/6 procedure belong in references |
| HIGH | Reference chains 2-3 deep, incl. a 649-line worker guide and the 552-line `harden-creation.md` |
| HIGH | Version-bound compatibility shims interleaved with the main path, no "old patterns" quarantine or removal markers |
| MED | `worker-provisioning-changes.md` orphaned from SKILL.md |
| MED | No TOC in any of six >100-line references |
| MED | Duplicated procedure text across SKILL.md and 5+ references (ceiling, licence, exit codes, catch-up sender, consume-report, re-take preconditions) |
| MED | Description in second person; triggers missing the app's own "update" vocabulary |
| MED | Inconsistent terms (worker/background agent; pass/run/flow; seven vs three bundles) |
| MED | Multi-branch shell procedures narrated instead of scripted; no checklist; audit is prose, not a validator |
| MED | Rationale paragraphs in the instruction path |
| LOW | `handoff-contract.md` is maintainer documentation shipped as a runtime reference; `heal-shallow-history.md` duplicates inline comments |
| LOW | No `compatibility` despite hard tool/Python requirements; unexplained `90m` timeout |

### Appendix B: build-app and update-app

#### Audit: `build-app` and `update-app`

Scope: `.agents/skills/build-app/` (SKILL.md + 5 references) and `.agents/skills/update-app/` (SKILL.md + references/critical-app.md). `python/` skipped. Rules cited: Anthropic best-practices page ("BP"), Academy course ("Academy"), agentskills.io spec/best-practices ("Spec"), repo's own `.agents/shared/references/spec-summary.md` and `validate_skill.py` ("Repo").

##### Headline numbers

| File | Lines | Words | ~Tokens (words x 1.35) |
|---|---|---|---|
| build-app/SKILL.md (body 868) | 875 | 7161 | ~9,670 |
| update-app/SKILL.md (body 534) | 540 | 4721 | ~6,370 |
| update-app/references/critical-app.md | 492 | 4189 | ~5,650 |
| build-app/references/cross-flow-gotchas.md | 100 | 626 | |
| build-app/references/verify.md | 93 | 458 | |
| build-app/references/frontend-choices.md | 27 | 371 | |
| build-app/references/cleanup.md | 20 | 109 | |
| build-app/references/public-url.md | 18 | 104 | |

Both SKILL.md bodies fail the repo's own `validate_skill.py` static check (`_MAX_BODY_LINES = 500`): build-app 868, update-app 534.

---

##### build-app

###### What it does well
- Has a real `references/` tier with on-demand files (cleanup, gotchas, frontend choices, public URL, verify), each linked from SKILL.md with a "when to read" cue (e.g. lines 559-578, 871-875).
- Clear hard gates and validation loops: the mock confirmation gate (360-406), "verify before surface" ordering (559-583), explicit confirmation-signal reading (652-664).
- One default per decision (Flask scaffolder canonical, escape hatch named as the exception), matching BP "one default plus escape hatch".
- Scripts are run, not read (`scaffold_flask_lib.py`, `verify_app.py`, `forward_port.py`), with full invocations.
- `cross-flow-gotchas.md` is symptom-indexed, as BP recommends for conditional detail.

###### Offenses

**B1. Body size: 868 lines, ~9.7k tokens (HIGH).** Rule: BP/Academy/Spec "keep SKILL.md under 500 lines", Spec "< 5000 tokens", Repo `validate_skill.py` 500-line cap. Evidence: `build-app/SKILL.md:9-875`. Nearly 2x the line limit and ~2x the token guidance. Remedy: move the reference-grade sections listed in B2 to `references/`.

**B2. SKILL.md is the full manual, not an overview + core workflow (HIGH).** Rule: BP "SKILL.md is a table of contents... move API reference, long examples, domain schemas to separate files"; Spec "concise, stepwise guidance with a working example outperforms exhaustive documentation". Sections that are reference material inlined into the main flow:
- "What gets generated" file-by-file manifest walkthrough, `build-app/SKILL.md:182-268` (~87 lines). One bullet (`app.toml`, 186-211) is a 26-line paragraph covering `stop_when_no_windows` semantics, routines, manifest validation and `[[references]]`; the `runner.py` bullet (230-264) is 35 lines on the shell page script, CSP modules and element-reference markup.
- "What gets updated and installed": program block + startsecs rationale + venv linking + secrets wrapper, `build-app/SKILL.md:270-345` (~75 lines).
- "The app describes its own API" OpenAPI/route-table contract, `build-app/SKILL.md:410-443` (~34 lines).
- CSP paragraph, `build-app/SKILL.md:459-477` (~19 lines).
- "Renaming an app", `build-app/SKILL.md:541-557` (17 lines; a rare side flow).
- "Escape hatch: wrap an existing server", `build-app/SKILL.md:679-766` (~88 lines) including three near-identical `[program:...]` blocks (690-697, 718-724, 734-740) and the shell-contract retrofit paragraph (756-766).
- "`forward_port.py` CLI reference", `build-app/SKILL.md:768-861` (~94 lines): a flag-by-flag CLI reference, the textbook case BP says goes in a reference file. Within it, the `--icon-file` bullet (790-833) embeds a 44-line icon-drawing style guide that already lives in `docs/system/app-icons.md` (which the bullet itself cites at 790 and 831).
- Step 4's `workspace-layout open/refresh` internals, `build-app/SKILL.md:591-628` (~38 lines), overlapping the `manage-desktop` skill it points to at 625.
Together these are roughly 450 lines of an 868-line body. Remedy: `references/scaffold-output.md`, `references/wrap-existing-server.md`, `references/forward-port-cli.md`, and shrink Step 4 to the two commands.

**B3. Eager "read first" load defeats progressive disclosure (MEDIUM).** Rule: Academy/Spec: supporting files are loaded "only when needed"; BP: a file read on every activation either belongs in SKILL.md or is oversized. Evidence: `build-app/SKILL.md:24` "**Read `.agents/shared/references/interactive-delivery.md` first.**" (143 lines) is unconditional. Line 363 "Read the reference `references/frontend-choices.md`" fires on every canonical-path run (27 lines). Effective unconditional load: 875 + 143 + 27 = **1,045 lines** before any conditional reference. The Step 5 hand-off additionally names `harden-creation.md` (552 lines) at 638 and 670, though that one is delegated to the worker. Remedy: inline the 5-6 skeleton phases build-app actually relies on (it already paraphrases them at 33-45) and drop the read-first.

**B4. Reference chain deeper than one level (LOW).** Rule: BP "keep references one level deep from SKILL.md". Evidence: `build-app/references/verify.md:63` -> `cross-flow-gotchas.md`; `build-app/references/cleanup.md:13` -> `.agents/shared/references/service-processes.md` (197 lines); `build-app/SKILL.md:535-537` -> `harden-creation.md` section "Keep rebuildable data out of the backup" (a 552-line file with no TOC). The two in-skill cases are also linked from SKILL.md, so they are tolerable; the harden-creation hop is a two-level jump into a very large file.

**B5. Duplicated text between SKILL.md and references (MEDIUM).** Rule: Spec "add what the agent lacks"; BP concision; a fact kept in two places drifts. Evidence:
- Bind-host guidance: `build-app/SKILL.md:131-138` vs `cross-flow-gotchas.md:90-100` (same content, same examples).
- Port-picking / `ss -tln` / port-in-use: `build-app/SKILL.md:117-130` vs `cross-flow-gotchas.md:75-88`.
- DNS-safe naming rules: `build-app/SKILL.md:100-111`, `:148-150`, `:780-786` and `cross-flow-gotchas.md:35-43` (four statements of one rule; the `forward_port.py` underscore allowance appears in only two of them).
- Verify recipe: `build-app/SKILL.md:559-571` restates `verify.md:15-27` (the file it links to).
- Public URL: `build-app/SKILL.md:863-869` restates `public-url.md` nearly in full (an 18-line file).
- `workspace-layout open --beside` semantics: `build-app/SKILL.md:591-599` and the `manage-desktop` skill.
- Icon rules: `build-app/SKILL.md:112-118` (pre-flight), `:790-833` (`--icon-file`) and `docs/system/app-icons.md`.
Remedy: keep one statement, link the rest.

**B6. Description frontmatter (LOW-MEDIUM).** Rule: BP "always write in third person"; Spec "what + when + keywords". Evidence: `build-app/SKILL.md:3` opens "Use when **you** want to create a new app..." (second person) and spends its second and third sentences describing the flow ("Runs an interactive flow: confirm the look and feel on a throwaway mock page...") rather than triggers. 435 chars, within limit. Trigger vocabulary a user would actually say ("make me a dashboard", "build a page", "a tool I can open", "new window") is thin; the user-facing words present are "page, dashboard, tool". Remedy: third person, lead with what+when, drop the flow summary.

**B7. Version-bound / environment-bound phrasing (LOW).** Rule: BP "avoid time-sensitive information"; BP "no voodoo constants". Evidence: `cross-flow-gotchas.md:39-40` "tolerates underscores for legacy names like `system_interface`"; `build-app/SKILL.md:120-126` hardcodes a port roster (8000, 8010, 8030, 8040, 8083, 8090) that goes stale as built-ins change, while the scaffolder already computes it (117-119); `verify.md:75` hardcodes `/opt/fortress/tilion-fortress/tilion`, an environment-specific path with no explanation. Remedy: point at the scaffolder's port check and the runtime; drop the roster.

**B8. Inconsistent terminology for one concept (MEDIUM).** Rule: BP "pick one term and use it throughout". Evidence: the thing being built is "app" (155 uses), "service" (34), "lib" (8: "scaffold a new Flask lib" line 19, "scaffolded lib" 873), "program" (26), "site" (Step 2/5 headings: "usable site", "working site"), "view" (Step 4 heading "Surface the view"), and "window". The background pass is "background harden worker" (633), "background worker" (631, 667), "generic worker" (669), "harden pass" (637). The phases are both "Step 0-5" and "skeleton phase 1-7" (lines 33-41, 366, 394, 630), forcing the reader to hold two numbering systems. Remedy: one noun (app), one worker name, one phase numbering.

**B9. Over-explanation / mechanism the reader does not need at run time (MEDIUM).** Rule: BP "Claude is already very smart... does this paragraph justify its token cost?"; Spec "explain the why" is for non-obvious rules, not mechanics. Evidence: why `bash -c` is needed under supervisord, stated twice (`build-app/SKILL.md:328-331`, `:690-693`); why `oom_tag_service.py user` prefixes, stated three times (331-334, 694-697, and the program-block comment); the venv-link + `--locked` + exec rationale (292-301); the `startsecs`/`startretries` back-off rationale (287-291) repeated in `cross-flow-gotchas.md:77-82`; Step 4's eight lines on how `--beside` nudges and resizes the chat (591-599); 11 lines on how `workspace-layout` POSTs to a loopback endpoint and broadcasts (604-614). Remedy: keep the rule, cut the mechanism, or move it to the reference the rule already cites.

**B10. Too many options without a default (LOW).** Rule: BP "provide one default plus escape hatch, not a menu". Evidence: escape-hatch program shapes, `build-app/SKILL.md:708-743`: "Two valid shapes: Inline (preferred when...) / Wrapper script (preferred for...)", two "preferred" options. Mock medium, `:371-380`: served page vs images in chat, with the images branch described at length. Minor.

**B11. Workflow structure weaknesses (LOW).** Rule: BP "sequential steps, a checklist for complex workflows, clearly signposted conditionals". Evidence: steps are numbered but out of order on the page: "Decide which path applies" (78) and "Pre-flight" (99) sit between Step 0 and Step 1; the mock gate is an H3 inside Step 1 (360) though the map at 33-45 presents it as its own step; the escape hatch (679) comes after Step 5 though it branches at Step 1. No copyable checklist for the 6-step flow despite BP recommending one for multi-step flows with gates. Remedy: a 6-item checklist at the top; move branches beside the step they fork from.

**B12. Reference files over 100 lines with no table of contents (LOW).** Rule: BP "for reference files longer than 100 lines, include a table of contents". Evidence: `cross-flow-gotchas.md` is exactly 100 lines with symptom headings and no TOC (borderline, acceptable). No build-app reference exceeds 100 lines. The shared files build-app sends the reader to do: `harden-creation.md` (552 lines), `service-processes.md` (197), `interactive-delivery.md` (143); none has a TOC. Noted because build-app depends on them; they are owned elsewhere.

**B13. Orphan / unlinked files: none.** All five references are linked from SKILL.md. File names are descriptive (`cross-flow-gotchas.md`, `verify.md`, `public-url.md`), satisfying BP naming guidance.

**B14. Missing package/install info (LOW).** Rule: BP "list required packages; don't assume tools are installed". Evidence: `verify.md:65-92` offers a Playwright script with no note that `playwright` is in the root venv (AGENTS.md says it is); `build-app/SKILL.md:140-149` relies on `uv`, `supervisorctl`, `ss` without stating availability. Minor in a controlled container image.

---

##### update-app

###### What it does well
- Strong front-door framing with a decisive routing step ("First, locate the app and read its manifest... follow critical-app.md and stop reading here", `update-app/SKILL.md:33-50`), i.e. a conditional reference used correctly.
- The live loop is a clear numbered 4-step cycle (150-331) with an explicit validation rule ("verify before the user can see it", 311-331).
- The data-protection section states one hard rule per bullet with the cheapest option first (332-481), and every script is run, not read.
- `critical-app.md` is a real conditional branch split out of the main file (BP Pattern 3), with its own numbered 1-4 structure and an explicit exit-code list (414-433).
- Out-of-scope routing is explicit (`build-app`, `manage-desktop`, `update-creation`, `heal-creation`).

###### Offenses

**U1. Body size: 534 lines, ~6.4k tokens (HIGH).** Rule: BP/Academy/Spec 500 lines, Spec < 5000 tokens, Repo `validate_skill.py`. Evidence: `update-app/SKILL.md:8-540`. Fails the repo's own validator. Remedy: see U2.

**U2. Reference-grade material inlined (HIGH).** Rule: BP "move advanced features / long workflows to separate files". Evidence:
- "Protect the user's data while you verify", `update-app/SKILL.md:332-481` (~150 lines, 28% of the body): the `serve_isolated_instance.py` invocation and flags (353-383), the `/tmp` RAM-disk warning (385-391), refresh-vs-down semantics (393-404), the full `preview_app.py` up/open/refresh/down block with venv-sync caveats and `--with`/`--instance-key` semantics (412-450), the snapshot procedure (461-473), retrofit instructions (475-481). This is a self-contained "verifying against a data copy" guide and belongs in `references/`.
- Step 1's `stop_when_no_windows` paragraph (`update-app/SKILL.md:160-175`) and the OpenAPI route-table paragraph (177-189) restate `build-app` content at reference depth; the latter is a single 13-line sentence chain.
Remedy: `references/verify-against-a-copy.md`; replace with a 6-line rule list and a link.

**U3. Eager cross-skill reads (MEDIUM).** Rule: Spec "resources loaded only when instructions reference them"; BP "one level deep". Evidence: `update-app/SKILL.md:57-59` "**read `.agents/shared/references/interactive-delivery.md`**" for every larger-scope change (143 lines); `:65-66` and `:85` cite its "phase 5 / Type 1 / Type 2 / phase 8" taxonomy, so the paragraph cannot be followed without that file. Effective load for a larger-scope change: 540 + 143 = 683 lines. For a critical app: the whole 540-line SKILL.md is already in context when line 50 says "stop reading here", plus `critical-app.md` 492 + `interactive-delivery.md` 143 + `harden-contention.md` 405 (cited at 142-144 and critical-app.md:395) = ~1,580 lines, none with a TOC. Remedy: summarize the two prototype types in one sentence each; make the critical-app routing happen from a 20-line top section so the ordinary flow is never loaded for a critical change.

**U4. Reference file > 100 lines without a table of contents (MEDIUM).** Rule: BP "for reference files longer than 100 lines, include a table of contents at the top". Evidence: `update-app/references/critical-app.md` is 492 lines / ~5.6k tokens (itself larger than the SKILL.md budget) with no TOC; its step 2 alone is 146 lines (124-269) and step 4 is 135 lines (351-485). Remedy: add a TOC; consider splitting go-live (351-485) into its own file since it is read once per pass.

**U5. Reference -> reference chains (MEDIUM).** Rule: BP "keep references one level deep". Evidence from `critical-app.md`: -> `interactive-delivery.md` (119-122), -> `type-app.md` "Critical apps" (129, 322), -> `op-update.md` (317), -> `update-creation` steps 1-3 (293-296, another skill's SKILL.md), -> `launch-task`'s `worker-failure.md` (331), -> `harden-contention.md` "Catching up a stale pass" (395), -> `update-self`'s `apply-outcomes.md` (426). A reader of the careful flow is sent to seven other files across four skills and the shared tree, several of which are 200-650 lines. From `update-app/SKILL.md`: -> `build-app/references/verify.md` and `cross-flow-gotchas.md` (303-307), another skill's second tier. Remedy: inline the 3-5 lines actually needed from each (the exit-code list at 414-433 already does this well) and link only for the rare path.

**U6. Description frontmatter (MEDIUM).** Rule: BP third person; Spec "a few sentences to a short paragraph"; every description sits in context permanently. Evidence: `update-app/SKILL.md:3` is 849 chars (the longest in the skill set; the mean is ~360), second person ("whenever the user asks **you**... load this BEFORE touching its code"), and more than half of it is instruction rather than trigger: "it owns the live change loop (apply the change so it takes effect, refresh the user's view, verify) and hands the change to the turn-end hardening flow, and it routes a critical app to the careful flow in references/critical-app.md". A file path inside a description is of no use at trigger time. Remedy: ~300 chars, third person, the verbs the user says (update/change/fix/restyle/extend/restart) + "apps and background services" + the build-app pointer.

**U7. Duplicated text across the two skills (MEDIUM).** Rule: Spec/BP concision; single source of truth. Evidence:
- `stop_when_no_windows` semantics: `update-app/SKILL.md:160-175` vs `build-app/SKILL.md:190-205` (near-verbatim, including the routines sentence).
- OpenAPI route-table obligations: `update-app/SKILL.md:177-189` vs `build-app/SKILL.md:410-443`.
- `workspace-layout open` "puts the window on screen the moment it returns / never tell the user to open it": `update-app/SKILL.md:271-279`, `critical-app.md:191-196`, `build-app/SKILL.md:580-583`, and AGENTS.md line 19.
- `/tmp` RAM-disk warning: `update-app/SKILL.md:385-391` and AGENTS.md line 238.
- "Removing a service": `update-app/SKILL.md:483-496` vs `build-app/references/cleanup.md` (which it links to).
- Verify-before-surface rationale: `update-app/SKILL.md:311-331`, `build-app/SKILL.md:559-583`, `critical-app.md:130-137`.
Remedy: one owner per fact (manifest semantics in a manifest reference; the window rule in `manage-desktop`), links elsewhere.

**U8. Inconsistent terminology (MEDIUM).** Rule: BP "consistent terminology". Evidence: the subject is "app", "service", "background service", "daemon" (195, 207-213, 294, 307-309), "program" (10, 236-242), "supervisord program". The prototype vocabulary switches between "hand mock", "Type 2", "Type 1", "demonstrative-prototype", "preview", "labeled preview window" within one section (`update-app/SKILL.md:52-80`). The turn-end pass is "harden pass", "hardening worker", "background worker pipeline", "generic harden worker" (498-540). Remedy: pick "app" / "background service", "preview", "harden pass".

**U9. Workflow numbering contradicts itself (LOW-MEDIUM).** Rule: BP "clear sequential steps". Evidence: `update-app/SKILL.md:271-279` admits "this is the one branch of the loop where the numbered order and that rule disagree, and the rule wins" (verify, step 4, must precede open, step 3). Step 2 also says "steps 2 to 4 of this loop are one call" (196-203), so the 4-step numbering is partly notional. Remedy: renumber as apply -> verify -> refresh/open, or fold into one step with two cases.

**U10. Over-explanation / rationale at the wrong layer (MEDIUM).** Rule: BP concision. Evidence: the `refresh_workspace_view.py` paragraph explaining why Imbue Studio does not intervene (`update-app/SKILL.md:287-294`); the data-isolation philosophy paragraph (406-410); "why this convention exists" asides inside the retrofit bullet (475-481); the "Does this change warrant a preview at all?" paragraph (68-79) spends twelve lines on a judgement call that could be one sentence plus the exception list. `critical-app.md:486-492` "Why this shape" repeats the rationale the file opened with at 10-39.

**U11. Time-sensitive / roster-bound text (LOW).** Rule: BP "avoid time-sensitive information; put it in an old-patterns section". Evidence: `update-app/SKILL.md:475-481` "Retrofit older services when you touch them. A service that predates this convention..." (migration-era text, no end condition, not sectioned off); `:243-248` the aside about `update-self` merges and `mngr start --restart system-services` is operational detail of another skill; `critical-app.md:447-451` names which built-in apps' bundles are replaced ("Getting Started, Files and Routines"), a roster that changes with the template. Remedy: move the retrofit note under an "older services" heading in the reference; drop app rosters.

**U12. Too many options (LOW).** Rule: BP "default + escape hatch". Evidence: for verification that writes, `update-app/SKILL.md:353-450` offers `serve_isolated_instance.py up ... --copy` (raw, "still the cheaper shape") *and* `preview_app.py up --app --worktree` (manifest-driven), with a paragraph at 441-450 explaining when each applies; the two tools, their refresh verbs and their venv caveats are described in parallel. Remedy: name `preview_app.py` the default for any app with a manifest; move the raw script to the no-manifest case in the reference.

**U13. Orphans / naming: none.** `critical-app.md` is linked from SKILL.md (43, 525) and the description, and is descriptively named. No unlinked files.

**U14. Missing install / dependency statement (LOW).** `update-app/SKILL.md:104-129` relies on `tk` without saying what it is or where it comes from; `critical-app.md:89-92` runs `npm ci && npm run build`, and 378 uses `jq`, with no availability note. Minor in the controlled image.

---

##### Cross-cutting summary

| Offense | build-app | update-app |
|---|---|---|
| Over 500 lines (BP/Academy/Spec/Repo validator) | 868 | 534 |
| Over ~5k tokens (Spec) | ~9.7k | ~6.4k (+5.6k critical-app.md) |
| Full manual instead of overview | ~450 lines of reference material inline | ~150 lines (data-copy guide) + manifest/API restatements |
| Unconditional "read X first" | interactive-delivery.md (143) + frontend-choices.md | interactive-delivery.md for larger-scope changes |
| Effective load, common path | ~1,045 lines | ~683 lines; ~1,580 for a critical app |
| Reference > 100 lines, no TOC | none owned (shared deps lack TOCs) | critical-app.md 492 |
| Reference -> reference chains | verify -> gotchas; cleanup -> service-processes | critical-app.md -> 7 files across 4 skills |
| Second-person description | yes | yes, and 849 chars with a file path in it |
| Duplicated content | 7 facts stated 2-4 times | 6 facts shared with build-app / AGENTS.md |
| Mixed terminology | app/service/lib/program/site/view; Step vs skeleton phase | app/service/daemon/program; mock/Type 1/Type 2/preview |
| Orphan files | none | none |

Out-of-scope note: the shared files both skills lean on (`interactive-delivery.md` 143, `service-processes.md` 197, `harden-contention.md` 405, `harden-creation.md` 552) have no TOCs and are the main reason the effective load is large; they are owned by `.agents/shared/`, not by these two skills.

### Appendix C: update-published-template, migrate-workspace, publish-template, use-template, update-installed-template

#### Audit batch: template and migration skills

Skills: `update-published-template`, `migrate-workspace`, `publish-template`, `use-template`, `update-installed-template`.
Rules cited: **[A]** platform.claude.com best-practices page, **[S]** agentskills.io spec / best-practices, **[C]** Academy course, **[R]** the repo's own `spec-summary.md` + `validate_skill.py`.
Body lines/words are the SKILL.md body after frontmatter; tokens are words x 1.35.

##### Cross-cutting offenses (all five)

| # | Offense | Rule | Evidence | Sev |
|---|---|---|---|---|
| X1 | The same ~45-line "fetch, merge in a throwaway worktree, boot-smoke-check with an inline Python heredoc, ff-only land" procedure is copied verbatim into two skills, and the latchkey-gateway `git fetch` incantation into three. | [A] one source of truth / consistent terminology; [S] "add what the agent lacks"; [R] shared refs exist for exactly this | `use-template/SKILL.md:95-163` vs `update-installed-template/SKILL.md:82-162` (32 identical lines >= 40 chars; the 16-line `PYEOF` block at 135-148 / 134-147 is byte-identical); gateway fetch at `use-template:103-105`, `update-installed-template:91-93`, `update-published-template:151-153`, already canonical in `connect-external-service/references/latchkey.md` | high |
| X2 | The "two unrelated v<n> axes" explanation (flow format v2 vs a template's publish count) is written out three times. | [A] duplicated info / terminology; [S] coherent units | `update-published-template/SKILL.md:26-28` + `references/v1-to-v2-migration.md:3-14`, `update-installed-template/SKILL.md:15-19`, `use-template/SKILL.md:10-20` | medium |
| X3 | Legacy v1 manifest handling is inlined into the live flow of four skills instead of a single "old patterns" reference. | [A] "no time-sensitive information (or in an 'old patterns' section)"; [S] overly comprehensive skills | `use-template:13-20,45-50,186-199,218-219,291-293`; `update-installed-template:15-19,51-53,181`; `update-published-template:97-100,217-223`; `publish-template:15-16,415-416` | medium |
| X4 | Three different step-numbering vocabularies inside one skill family: `§2e` (update-published-template), `step 9` (publish-template), `Step 3` (migrate-workspace), `§0`/`2b` (use-template, update-installed-template). Cross-skill references mix them ("`use-template` §1", "`publish-template`'s step 3"). | [A] consistent terminology | `update-published-template/SKILL.md:47-64,423-427`; `publish-template/SKILL.md:31,386`; `migrate-workspace/SKILL.md:139,264` | low |
| X5 | The same `latchkey curl -XPOST .../permission-requests` JSON body is re-typed per skill (5 copies in this batch) rather than pointed at `connect-external-service/references/latchkey.md`. | [A] one default + reference, not re-explanation | `publish-template:314-323`, `update-published-template:484-487`, `migrate-workspace:87-94`, `use-template:238-245` (prose copy) | low |
| X6 | Incident history baked into instructions ("it once reset a live agent's tree", "a merge of it once deleted 1400+ files"). | [A] no time-sensitive info; explain the why, not the war story | `update-published-template:55-56`; `publish-template:29-30` | low |
| X7 | "Companion skill" preambles restate the whole family's roles in every member. | [A] concise is key; Claude already has the catalog descriptions | `update-published-template:10-13`, `update-installed-template:10-13`, `publish-template:3` (desc) | low |

##### 1. update-published-template

**Size.** Body 552 lines, 4625 words, ~6240 tokens. Over the 500-line limit [A][S][C][R] (fails `validate_skill.py`) and over the 5000-token guidance [S]. References: `invariants.md` (22), `v1-to-v2-migration.md` (63). Effective eager load: SKILL.md (558) + `references/invariants.md` ("read it before the push", L558) = 580 lines; the lead also follows `lead-proxy.md` (409) and `worker-reporting.md` (236) by reference.

| # | Offense | Rule | Evidence | Sev |
|---|---|---|---|---|
| 1 | Body over 500 lines; fails the repo's own validator. | [A][S][C][R] | 552 body lines | high |
| 2 | The entire worker procedure (a 10-step git recipe the lead must transcribe into a task body) is inlined in SKILL.md instead of living in a worker reference the way `publish-template/references/assembly-worker.md` does. | [A] progressive disclosure: "push long workflows into separate files"; [S] resources loaded on demand | `SKILL.md:319-421` (~100 lines, "The task body directs the worker to: 1...10") | high |
| 3 | A fragile, destructive, strictly-ordered operation (reset to published tip, overlay, re-scan, mint commit) is prose rather than a script, explicitly because `build_template.sh` has no `--update` mode. This is the low-freedom case [A] says to script. | [A] "solve, don't defer"; low freedom = exact scripts; [S] "bundle scripts when the agent keeps reinventing the same logic" | `SKILL.md:276-281` ("There is no `build_template.sh --update` mode ... the worker performs the re-assembly with explicit git steps"); steps at 333-414 | high |
| 4 | 46 lines of shouting callouts before the first step (three blockquotes, 74 ALL-CAPS emphasis words in the body). | [A] concise; [S] "explain the why ... works better than rigid directives (ALWAYS/NEVER)" | `SKILL.md:30-75`; caps count 74 vs 4-17 in the sibling skills | medium |
| 5 | Sections that are detail, not core workflow, sit inline: `## Shared conventions` (77-102, 26 lines), 2b out-of-band divergence check with awk/bash (157-210, 54 lines), 3a bundle rationale (283-300), §4 guard rails (431-441). | [A] SKILL.md = overview + core workflow | listed line ranges, ~110 lines total | medium |
| 6 | `references/invariants.md` is a 22-line restatement of what the top-of-file callouts already say; the body repeats the list again at 552-558. Three copies of the same invariants. | [A] duplicated info | `SKILL.md:30-75`, `SKILL.md:552-558`, `references/invariants.md` | medium |
| 7 | Reference file `v1-to-v2-migration.md` is linked from SKILL.md (one level, fine) but its content is partly repeated inline (26-28, 217-223). | [A] one level + no duplication | see lines | low |
| 8 | Description is second person ("a template **you** already published"); otherwise has what + when and trigger verbs (update, re-publish, revise, ship). 396 chars. | [A] third person | frontmatter L3 | low |
| 9 | Delegates to `publish-template`'s references in six places (`failures.md`, `assembly-worker.md`, `references/v1-to-v2...`), so the real reference graph is two skills deep: SKILL.md -> publish-template/references/*.md -> scripts. | [A][S] references one level from SKILL.md | `SKILL.md:329-332,439,461-462,481,516` | low |
| 10 | Time-bound text: "whose launcher may predate the `report` subcommand" (331), "a first publish has none, so in practice the first update creates one" (514). | [A] no time-sensitive info | lines cited | low |

**Does well.** Hard gates are explicit with "end your turn and wait"; every failure path says STOP rather than improvise; the `done` handling override of `lead-proxy.md` is stated once at the top and again where it applies.

##### 2. migrate-workspace

**Size.** Body 541 lines, 3889 words, ~5250 tokens. Over 500 lines (fails `validate_skill.py`); slightly over 5000 tokens. References: `agent-recreation.md` (173, no TOC), `pre-declutter-layout.md` (277, no TOC). Effective load, lead on a pre-declutter source: 550 + 277 ("keep it open for the rest of the pass", L188-192) = 827 lines, plus `harden-contention.md` (405) and `lead-proxy.md` (409) followed by reference. Worker: told to follow "SKILL.md Step 7" (L304), so it loads the whole 550-line lead file plus `agent-recreation.md` (173) + `pre-declutter-layout.md` (277, "read it first") + `worker-reporting.md` (236) = ~1236 lines.

| # | Offense | Rule | Evidence | Sev |
|---|---|---|---|---|
| 1 | Body over 500 lines; fails the repo's validator. | [A][S][C][R] | 541 body lines | high |
| 2 | Lead and worker halves share one SKILL.md; the worker is pointed at "Step 7" of the lead's file, so the worker eagerly loads 430 lines it never uses. `publish-template` solved the same shape with `references/assembly-worker.md`. | [A] progressive disclosure; [S] load resources only when the instructions reference them | `SKILL.md:304-305` (task body: "Follow `.agents/skills/migrate-workspace/SKILL.md` Step 7"), `SKILL.md:338-454` (117 lines of worker-only content) | high |
| 3 | Both reference files exceed 100 lines with no table of contents. | [A] "for reference files longer than 100 lines, include a table of contents" | `references/agent-recreation.md` (173), `references/pre-declutter-layout.md` (277) | medium |
| 4 | Version-bound text in the live flow rather than the old-patterns reference: the opening paragraph is a changelog (`minds-v0.3.9`, Debian 12 -> 13); "An older `host-backup-now`" waiter behavior; "the layout of older workspaces ... until it does, `validate_skill.py` rejects it"; "`run_job.sh` ... which no longer exist". | [A] no time-sensitive info outside an old-patterns section | `SKILL.md:11-16,146-152,398-401,415-419` | medium |
| 5 | Step 3 "Preconditions" is a 68-line grab-bag (single-flight, two backgrounded backups, bounded waits, freshness check, pinning, quiescence) and Step 7 is 117 lines; both are detail that belongs in references with a one-line pointer. | [A] SKILL.md = overview + core workflow; split near the limit | `SKILL.md:112-179`, `338-454` | medium |
| 6 | The 90-character module path `.agents/skills/migrate-workspace/python/migrate_workspace_skill/migrate_workspace.py` is written out 8 times; an alias (`$M`) is introduced only in Step 5 and then abandoned. | [A] concise; consistent terminology | 8 occurrences; alias at `SKILL.md:226` | low |
| 7 | Description uses first-person user quotes ('move my stuff over', 'I made a new agent', "I'd like to move") inside what is otherwise third-person. They are trigger phrases, so the harm is small, but the page says keep one point of view. 551 chars. | [A] third person | frontmatter L3 | low |
| 8 | Explanatory asides ("That is what makes auto-porting ... safe", "Recreation is cheap and reversible", "That is a legitimate outcome, not a failure") pad the body; much of it is reasoning the model does not need to execute the step. | [A] "does this paragraph justify its token cost?"; [S] omit what the agent knows | `SKILL.md:18-25,205-210,212-217,264-268` | low |
| 9 | Deep chain: SKILL.md -> `update-self/references/python-packaging-migration.md` (two named sections) and -> `connect-external-service/references/latchkey.md`, -> `launch-task/references/worker-failure.md`, i.e. references inside other skills' reference dirs. | [A][S] one level deep from SKILL.md | `SKILL.md:258,372,396,464` | low |

**Does well.** The pre-declutter material is correctly quarantined in its own reference that opens with "Load this only when `detect-layout` reported `pre-declutter`"; mechanical steps are scripted subcommands with exit codes the prose interprets; every scan has a stated `caveat` to read; the repeat-run section makes idempotence explicit.

##### 3. publish-template

**Size.** Body 409 lines, 2990 words, ~4040 tokens. Under both limits but within 91 lines of the cap. References: `assembly-worker.md` (191, has a TOC), `failures.md` (75), `readme-recipe.md` (96). Effective load, lead: 417 + `lead-proxy.md` (409) + `failures.md` on demand. Worker: `assembly-worker.md` "Before anything else, read ... in your checkout and follow it" (`SKILL.md:200-202`) + `readme-recipe.md` "read it now, before step 1 removes it" (`assembly-worker.md:147-148`) + `worker-reporting.md` (236) = ~523 lines before the worker starts.

| # | Offense | Rule | Evidence | Sev |
|---|---|---|---|---|
| 1 | Four of five `scripts/` files are not named anywhere in SKILL.md; `build_template.sh`, `scan_secrets.sh`, `betterleaks.toml` are reached only via `references/assembly-worker.md` / `failures.md`, and `write_template_manifest.py` only from inside `build_template.sh`. Two-hop reachability for the skill's primary executable. | [A] "all reference files should link directly from SKILL.md"; name scripts and whether to run them | grep: `build_template.sh` appears in `references/assembly-worker.md:47`, `failures.md:9`, `readme-recipe.md:4`, never in `SKILL.md`; `write_template_manifest` only in `scripts/build_template.sh` | medium |
| 2 | Reference-to-reference chains: `assembly-worker.md` -> `readme-recipe.md` (L147), `assembly-worker.md` -> `scripts/*` + `.agents/shared/references/worker-reporting.md`; `readme-recipe.md` -> `publish-template`'s step 9 (back-reference into SKILL.md) and -> `agentic-browser-fleet` skill. | [A] keep references one level deep | `references/assembly-worker.md:147,182-185`; `references/readme-recipe.md:82,95` | low |
| 3 | The `readme-recipe.md` worker/lead split is enforced by prose in two places ("Do not show the README to the user; the lead does that" vs "driven by the LEAD -- never by the assembly worker"). The same file serves two audiences at once. | [A] organize for discovery; one reader per file | `assembly-worker.md:151`; `readme-recipe.md:46-53` | low |
| 4 | Step 0 offers a menu ("Another platform (GitLab, Codeberg, self-hosted) ... research that before promising anything") instead of one default plus an escape hatch. | [A] too many options; [S] defaults not menus | `SKILL.md:95-100` | low |
| 5 | Two inline latchkey permission-request JSON bodies plus a paragraph on why the permission names must be exactly those, duplicating `connect-external-service/references/latchkey.md` and `github_access.py`'s own messages. | [A] duplicated info; scripts should carry the error handling | `SKILL.md:313-327` | low |
| 6 | Incident history and version-bound notes inline: "a merge of it once deleted 1400+ files"; "adopters still read v1 repos ... but nothing writes them"; `## Publishing over an earlier template` closes on "any v1 manifest ... has no `[origin]`". | [A] no time-sensitive info | `SKILL.md:29-30,15-16,415-417` | low |
| 7 | Description ends with a routing sentence to a sibling skill ("For a new version ... use update-published-template"), which is an instruction rather than a trigger and costs catalog tokens on every turn. 425 chars. | [A] description = what + when; [S] catalog entry ~50-100 tokens | frontmatter L3 | low |

**Does well.** This is the best-structured skill in the batch: a copyable `- [ ]` checklist of the ten steps (L64-76) with the two "end turn, wait" gates marked; the worker's instructions are a separate reference with a TOC; failures are a table keyed by exit code in their own file; the three governing rules are stated once up front with reasons; the "values carried through the flow" block names every variable the fresh-shell model loses.

##### 4. use-template

**Size.** Body 356 lines, 2837 words, ~3830 tokens. Under both limits. No `references/` directory at all: every branch is inline.

| # | Offense | Rule | Evidence | Sev |
|---|---|---|---|---|
| 1 | No progressive disclosure: the merge mechanics (§1, 86 lines including a 16-line Python heredoc), the manifest format taxonomy (§2, 54 lines), the environment-install recipe (§3.2, 33 lines) and the override/lineage rules (§6) are all inline though each applies to only one path. | [A] move advanced features / long workflows to separate files; [S] resources on demand | `SKILL.md:78-171,173-225,246-277,329-354` | medium |
| 2 | v1 handling interleaved throughout the live flow (5 separate places) rather than a single "older manifests" reference. | [A] old patterns in their own section | `SKILL.md:13-20,45-50,124-130,186-199,218-219,291-293` | medium |
| 3 | Shares ~45 lines verbatim with `update-installed-template` (trust gate, worktree merge, boot smoke-check heredoc, "An app from an older template", ff-only landing, resolve-requirements, append-entry, commit). | [A] duplication; [R] shared references exist for this | `SKILL.md:57-76` ~ `update-installed-template:29-44`; `SKILL.md:108-163` ~ `update-installed-template:96-162`; `SKILL.md:124-130` = `update-installed-template:123-129` word for word | high |
| 4 | References a `welcome` skill (`.agents/skills/welcome/SKILL.md`) that does not exist in this template; the text means "the template repo's", which a reader cannot tell. | [A] consistent terminology; name files descriptively | `SKILL.md:48,189`; `ls .agents/skills/welcome` fails | low |
| 5 | Description lacks the trigger keywords users actually type: the published README's copy line is ` /use-template <url>` and the manifest is called `template.md` / "inspiration", none of which appear. 241 chars, with room. | [A] include key terms; [C] add keywords matching how people phrase requests | frontmatter L3; cf. `publish-template/references/readme-recipe.md:91` | low |
| 6 | Mixed dash conventions (em-dash `—` in §A/§B/§3, `--` elsewhere) and "adopt"/"adapt" used interchangeably in headings and body. | [A] consistent terminology | `SKILL.md:27-28,37,53,230-231` vs 83, 96 | low |
| 7 | Inline Python heredoc for the supervisord boot check is a script embedded in prose, copied into a sibling; the same check is described a third time in prose in `update-published-template:391-397`. | [A] bundle utility scripts; one source of truth | `SKILL.md:135-148` | medium |

**Does well.** The two entry points are disambiguated before any work; the trust gate is an explicit, well-reasoned stop; activation-vs-adaptation is defined once and reused; "definition of done" for a data-backed app is concrete.

##### 5. update-installed-template

**Size.** Body 246 lines, 1962 words, ~2650 tokens. Under both limits. No `references/`.

| # | Offense | Rule | Evidence | Sev |
|---|---|---|---|---|
| 1 | Roughly half the body is a copy of `use-template` with "newer version" substituted (see use-template #3). The skill-specific content is ~60 lines (§0's second paragraph, §1, the `--allow-unrelated-histories` warning, §2b, §5). | [A] duplication; [S] coherent units (this is a mode of `use-template`, not a separate body of knowledge) | `SKILL.md:29-44,66-162,183-203,241-252` | high |
| 2 | §5 is a 35-line hand-written ledger-append contract ("There is no helper skill: this block is the whole recording contract") that reaches into `update-self`'s Python for the starter block, while `publish-template` has `record_publish.py` for the publisher side. Prose is doing a script's job. | [A] "solve, don't defer"; low-freedom operations get exact scripts | `SKILL.md:205-239`, pointer at 218-220 | medium |
| 3 | v1/v2 axis preamble duplicated (X2); v1 no-ops called out in-line. | [A] old patterns section | `SKILL.md:15-19,51-53,181` | low |
| 4 | Inline boot-check heredoc identical to `use-template`'s. | [A] bundle scripts | `SKILL.md:133-148` | medium |
| 5 | Description is fine on voice and has what + when (update, upgrade, pull the latest); 244 chars. No offense beyond lacking `template/<slug>/v<n>` or "new version" keywords. | -- | frontmatter L3 | none |

**Does well.** The one genuinely new rule (never pass `--allow-unrelated-histories`; unrelated histories means a wrong URL) is stated with its reason; "do not remove packages the newer version dropped" is a good, specific guard; idempotence of the ledger entry is scoped and explicit.

##### Summary by severity

- **High (6):** update-published-template over 500 lines with the worker recipe inlined and a destructive 10-step git procedure left as prose; migrate-workspace over 500 lines with lead and worker in one file so the worker loads everything; use-template / update-installed-template duplicate ~45 lines of merge-and-verify procedure verbatim.
- **Medium (14):** no TOC on 173- and 277-line references; legacy v1 content scattered through live flows in four skills; triple-stated invariants; 46-line all-caps callout block; inline boot-check script copied three ways; publish-template's primary script reachable only two hops from SKILL.md; prose ledger contract; no `references/` dir in use-template despite single-path sections.
- **Low (20):** description voice (second/first person in two skills), routing sentences in descriptions, three step-numbering styles in one family, cross-skill reference chains into other skills' `references/`, incident history, repeated long paths, a `welcome` skill that does not exist here.

Out of scope note: `.agents/shared/references/lead-proxy.md` (409 lines) and `harden-contention.md` (405) are pulled in by three of these skills and have no TOC either; Appendix G covers them.

### Appendix D: manage-desktop, agentic-browser-fleet, update-creation, caretaker, crystallize-creation, fetch-process-show, github-sync, data-pipeline-builder, use-ai-integration, heal-creation

#### Audit: medium-size dwt skills (batch of 10)

Scope: `manage-desktop`, `agentic-browser-fleet`, `update-creation`, `caretaker`, `crystallize-creation`, `fetch-process-show`, `github-sync`, `data-pipeline-builder`, `use-ai-integration`, `heal-creation`. SKILL.md plus `references/*.md`; `python/` not reviewed. Line numbers are SKILL.md lines unless a path is given. Token estimates are body words x 1.35.

Sources cited: [AN] platform.claude.com best-practices; [AC] academy course; [AS] agentskills.io spec / best-practices; [REPO] `.agents/shared/references/spec-summary.md`, `type-skill.md`, `validate_skill.py`.

Shared-reference sizes that matter for "effective load" (wc -l): `lead-proxy.md` 409, `harden-contention.md` 405, `worker-reporting.md` 236, `interactive-delivery.md` 143, `user-facing-language.md` 117, `shared/worker/SKILL.md` 112, `shared/worker/references/harden-creation.md` 552. None of the shared files over 100 lines has a table of contents (lead-proxy 13 headings, harden-contention 7, worker-reporting 11, harden-creation 19).

##### Cross-cutting findings for this batch

1. **The harden-lifecycle trio (crystallize / heal / update-creation) is three copies of one flow.** `heal-creation` and `update-creation` share 8 verbatim runs (40 lines): type-parameter intro (heal 23-25 = update 59-61), `$CREATIONS` conventions (45-47 = 78-80), single-flight check (56-58, 64-72 = 89-91, 97-105), ticket heredoc (75-78 = 108-111), task-file frontmatter (99-101 = 149-151), the "commit, never stash" block (136-140 = 200-204; also crystallize 171-173), and the whole Step 4 merge-time paragraph (168-177 = 235-244). update and crystallize also share the launch block (206-212 = 175-181). The go-live-by-type list (skill sync / app refresh / service restart with the `host_backup` and `oom_priority` program names) is written out three times: heal 180-191, update 249-263, crystallize 239-252. Medium-high. Remedy: one shared `harden-lead.md` reference with the common steps, each SKILL.md keeping only its operation, gate list and substitutions (the repo already did this once for the proxy mechanics, per `docs/system/specs/skill-lifecycle-shared-refs/concise.md`; the dispatch half was left behind). [AN consistent terminology / concise; AS "design coherent units"]
2. **Effective load of the trio is 4-6x the SKILL.md.** Each lead tells the agent to follow `harden-contention.md` (405) twice, `lead-proxy.md` (409), and `worker-reporting.md` (236) in the normal path, so a heal run reads ~1,250 lines of reference on top of a 203-line SKILL.md; update-creation ~1,330 on top of 282. All three shared files exceed 100 lines with no table of contents, and they chain onward (`lead-proxy.md` -> `harden-contention.md`, `worker-failure.md`, `dead-worker-recovery.md`, `freeing-memory.md`, `report-built-in-issues.md`, `worker-reporting.md`; `harden-contention.md` -> `harden-creation.md` (552), `dead-worker-recovery.md`, `critical-app.md`). High. [AN "references one level deep", "TOC for reference files over 100 lines"; AS "avoid deeply nested reference chains"]
3. **Scripts told to be copied, not run.** `use-ai-integration` 124, 152-153 say "copy this skill's `claude_p.py`" into each consumer. [AN "execute scripts rather than read them"; REPO spec-summary says a consumer should depend on `<name>-skill` as a workspace package]. Medium.
4. **Descriptions are instructions, not triggers.** Five of ten are second-person or imperative and contain workflow detail that belongs in the body (caretaker 717 chars, fetch-process-show 526, agentic-browser-fleet 443 with "YOU own the browser and YOU drive it", manage-desktop "Use when you want to ...", crystallize/heal/update-creation "Invoke at turn-end ..."). [AN "write in third person", "what it does + when to use it", avoid injecting instructions]. Medium.

---

##### manage-desktop (SKILL.md 327 lines, ~4,800 tokens, no references)

What it does well: one command surface (`workspace-layout`), a "95% verbs" table (136-150), exit codes (311-321), a "when NOT to use" section, and a clear live-effect warning (152-158).

| # | Rule | Evidence | Sev |
|---|---|---|---|
| 1 | Progressive disclosure: SKILL.md is the full manual, not an overview [AN, AS] | No `references/` at all. Sections that are lookup material, not core workflow: "Popped-out windows" 81-120 (40 lines, incl. a 7-row op-by-op table), "Shortcuts and the wallpaper" 215-255 (41 lines), "Inspecting state" field dictionary 257-275 (19), "What is not here" 277-296 (20), "Ops answer at once" 298-309. ~130 lines (40%) is reference. | high |
| 2 | Concision / "does this paragraph justify its token cost" [AN] | "The vocabulary (read this first)" 28-43: 7-row table with 50-100-word cells, e.g. the `launch path` cell (36) and `window` cell (33); 160-180 is one 20-line paragraph of path recipes; 71-79 `--beside` geometry. Restates what `--help` and `desktops` output already show. | medium |
| 3 | Description is second person and lists verbs rather than triggers [AN] | line 3: "Use when you want to open, arrange, or close windows ... (open an app or a page, show a page, focus, place, minimize, maximize, close, navigate, refresh)". 284 chars; no third-person "what it does" sentence. | low |
| 4 | Time-sensitive / drift-prone content [AN] | 238 "just `arcs` today"; 285 "the terminal offers no rename route yet"; 293-296 a list of "old tabbed-shell verbs" and spellings the tool refuses (legacy content inline, not in an "old patterns" section). | medium |
| 5 | Inconsistent terminology [AN] | "op" / "verb" / "command" / "subcommand" used interchangeably (45, 136, 104, 249); "client" vs "screen" vs "browser" for the same thing (35, 3, 223). | low |
| 6 | Workflow structure [AN] | No numbered procedure or checklist; the skill is a table-plus-prose reference, so the common flow ("find the requester, open beside, verify") must be assembled by the reader. | low |

##### agentic-browser-fleet (SKILL.md 303 lines, ~3,800 tokens, no references)

What it does well: explicitly declines to restate `playwright-cli --help` (25-28), a 4-step loop (62-67), fleet exit-code table with actions (253-266), quick recipes (268-288), a "Don'ts" list, and strong gotchas (virtualized lists 150-175, scroll target 177-182).

| # | Rule | Evidence | Sev |
|---|---|---|---|
| 1 | Progressive disclosure [AN, AS] | No `references/`. Conditional / rare material inline: "Crashes, stops, and the Screen going away" 132-148, "Long lists" 150-175, "Snapshots vs screenshots" 184-203, "Live view" 221-241, "Tabs and sub-agents" 243-251. ~100 lines could be `references/` loaded on the matching symptom ("Read `references/recovery.md` when `ls` says crashed/stopped"). Core loop + ownership rules + exit codes would fit in ~120 lines. | medium |
| 2 | Description: second person, imperative, carries instructions [AN] | line 3: "Drive the workspace's shared Chromium browser yourself ... load that skill first rather than this one. YOU own the browser and YOU drive it -- in this same chat, with your own reasoning." The routing instruction ("load connect-external-service first") and the emphasis belong in the body. | medium |
| 3 | Duplicated content within the file | Ownership rules 93-116 restated in "Don'ts" 290-303 (reclaim, CAPTCHA, re-attach) and in the exit-code table 262; the "run `ls`" rule appears at 118-130, 134-136, 298. | low |
| 4 | Overlap with sibling skills [AS coherent units] | "Live view" 221-241 re-explains client targeting that `manage-desktop` 45-54 owns, with a different wording ("the client that last messaged your chat while it is connected, else the one connected client if it is the same user's ..."). | low |
| 5 | Voodoo constants / magic numbers [AN] | "~60s" idle lease (100), "30s" timeout (122), "20 rows per 800px" (174), "5-15k tokens" (187) stated without a source; fine as heuristics but unsourced. | low |
| 6 | Name no longer matches behavior [AN naming] | Skill and command are `agentic-browser-fleet`; body says the fleet is capped at one Chromium and `new <name>` must not be used (50-52). | low |

##### update-creation (SKILL.md 282 lines, ~2,700 tokens, no references of its own)

What it does well: numbered Steps 1-4 (89-274), a worked task-file template (143-183), explicit "Flow-specific substitutions" (222-233), an explicit "commit before launch" guard.

| # | Rule | Evidence | Sev |
|---|---|---|---|
| 1 | Duplication with `heal-creation` (see cross-cutting 1) | 8 verbatim runs, 40 lines; plus near-verbatim go-live list 249-263 vs heal 180-191 and crystallize 239-252. | high |
| 2 | Eager loads / effective context (cross-cutting 2) | Step 1 (93) and Step 4 (238) both say run the checks in `harden-contention.md` (405 lines, no TOC); Step 3 (221) "follow `lead-proxy.md`" (409); task file (175) sends the worker to `shared/worker/SKILL.md`. Effective lead-side load ~1,330 lines for a 282-line skill. | high |
| 3 | Reference chain deeper than one level [AN, AS] | SKILL.md -> `lead-proxy.md` -> `harden-contention.md` / `worker-failure.md` / `dead-worker-recovery.md`; SKILL.md -> `harden-contention.md` -> `harden-creation.md` (552). | high |
| 4 | Progressive disclosure: edge cases inline [AN] | "The type parameter" critical-app carve-out 65-73 and the second carve-out 190-198 ("One exception, and it lives here ...") are branch-specific detail that reads to every caller; the `oom_priority` program-name list 256-263 is a service-specific lookup. | medium |
| 5 | Description: second person, imperative, contains the trigger rule rather than a what+when sentence [AN] | line 3: "Change an existing skill ... Invoke at turn-end when a skill ran but you had to do extra repeatable work by hand, or when you and the user discussed ...". | low |
| 6 | Terminology [AN] | "pass" / "change" / "update" / "harden pass" for the same unit (15-26); "origin" here vs "path" in `op-update.md` (71, 105); "ticket" via `tk` vs "task file". | low |
| 7 | Workflow validation loop [AN] | No verification step after merge ("confirm the window refreshed / the service restarted cleanly"); go-live ends at `tk close`. | low |

##### caretaker (SKILL.md 274 lines, ~3,700 tokens, no references)

What it does well: an explicit first-run/later-run branch (27-40), numbered run procedure (176-261), the welcome message is given verbatim (99-123), permissions file template inline (137-147).

| # | Rule | Evidence | Sev |
|---|---|---|---|
| 1 | Description is 717 chars of spec; second half is the whole procedure [AN "specific and concise", AS "a few sentences"] | line 3 enumerates first-run behavior, every weekly check, "all with permission; reviews the previous run, proposes (or, with permission, applies) fixes and commits, and summarizes, always in plain user-experience terms". It also hardcodes a script path (`system/services/caretaker/caretaker_check.sh`). Second-longest description in the repo; ~180 tokens in every agent's catalog. | medium |
| 2 | Progressive disclosure [AN] | Material that should be separate files: the verbatim welcome message 99-123 (an asset / template), the permissions-file template 137-147 (asset), "Recording the user's choices" 149-174 (branch taken only when the user answers). Core run (176-261) plus the branch rule would be ~120 lines. | medium |
| 3 | Duplication within the file | The three checks are spelled out twice: first-run 77-85 and the run 210-231; the identical `workspace-layout show chat ...` invocation at 127 and 182; "how you talk to the user" 42-65 restated in step 1 (178-196) and step 5 (251-258). | medium |
| 4 | "(read this first)" on an in-file section | 42: an eager-read marker on a section of the file already being read. | low |
| 5 | Terminology [AN] | 10-25 and 178-203 "the routines runner clears your chat and sends `/caretaker`"; description calls it a "deterministic weekly check"; AGENTS.md calls it a routine; three names for one trigger. | low |
| 6 | Voodoo constants [AN] | 218 "disk above ~85 percent", 260 "30 most recent logs", no rationale. | low |
| 7 | Implementation-bound wording | 199 "it sends `/clear`" is a runner implementation detail the skill cannot observe; drift risk. | low |

##### crystallize-creation (SKILL.md 261 lines, ~2,330 tokens; refs: `when-to-crystallize.md` 96, `post-crystallize-migration.md` 148)

What it does well: references are conditionally loaded ("Read `references/when-to-crystallize.md` if you haven't" 39; migration checklist loaded only on `done` 241); numbered Steps 1-6; the migration reference is itself a numbered checklist and says "stop reading once the items don't apply".

| # | Rule | Evidence | Sev |
|---|---|---|---|
| 1 | Reference >100 lines without a TOC [AN] | `references/post-crystallize-migration.md` is 148 lines, 6 `##` sections, no contents list. | low |
| 2 | Eager loads / chains (cross-cutting 2) | Step 5 (208) "Follow `lead-proxy.md`" (409); task file (139) "Read and follow `shared/worker/SKILL.md`" which loads `harden-creation.md` (552) + `op-crystallize.md` + `type-*.md`. SKILL.md -> `lead-proxy.md` -> `harden-contention.md` is two levels. | high |
| 3 | Duplication with heal/update (cross-cutting 1) | 171-181 (commit-before-launch + launch block) = update 202-212; go-live list 239-252. | medium |
| 4 | Progressive disclosure: a 56-line heredoc inline (104-161) plus 23 lines of frontmatter prose (83-102) | The `diff_base` derivation for app/service (89-95) is a `git log --diff-filter=A` recipe wedged in a paragraph; a template under `assets/` plus a short fill-in list would halve Step 3. | medium |
| 5 | Three entry modes braided into one file [AS coherent units] | Standalone skill, `type=app` from build-app, `type=skill` from fetch-process-show: each step carries "(skill, standalone only)" / "when invoked by `build-app`" / "if a wrapper handed you ..." carve-outs (47-52, 163-167, 188-189, 206-207, 220-226). No branch map at the top ("Standalone? -> Steps 1-6. Wrapper? -> Steps 2-6"). | medium |
| 6 | Description: imperative, no third-person "what it does" [AN] | line 3 "Promote the just-finished work into a new, reusable, committed, tested skill. Use when ...". Triggers are fine ("crystallize this"); voice is the issue. | low |
| 7 | Stale / hedged content in a reference | `post-crystallize-migration.md` 38-41 "fallback can stay until the next refactor pass ... Note in the PR description" (there is no PR in this flow); 127-130 "`tk` does not have a list command in this build" (version-bound). | low |
| 8 | Reference duplicates SKILL.md and spec-summary | `when-to-crystallize.md` 41-52 restates the `[script]/[ai-script]/[prose]` model from `spec-summary.md`; SKILL.md 37-45 restates the reference's own summary. | low |

##### fetch-process-show (SKILL.md 254 lines, ~2,800 tokens, no references of its own)

What it does well: concrete gating rules (sample must demonstrate every shape 111-131; hard gate 167-170), a plan template (64-73), the cost gate (150-163), explicit handoffs to `connect-external-service`, `crystallize-creation`, `build-app`.

| # | Rule | Evidence | Sev |
|---|---|---|---|
| 1 | Eager load of a second file to understand this one [AN overview-first] | 16: "**Read `.agents/shared/references/interactive-delivery.md` first.**" (143 lines). Every section heading is then keyed as "(skeleton phase N)" (36, 79, 101, 167, 199), so this file cannot be followed without the other open. Effective load 397 lines; the skeleton's 8 phases are never listed here. | medium |
| 2 | Chain: SKILL.md -> `crystallize-creation` Step 5 -> `lead-proxy.md` (180-181, 251) | Two-level indirection to find the poll command. | medium |
| 3 | Description is 526 chars, carries a routing rule and a quoted example [AN] | line 3 "... Start here even when the ask names the surface rather than the data (\"build me a dashboard over this dump\"): this skill hands build-app the confirmed sample." | low |
| 4 | Progressive disclosure: optional branches inline [AN] | "Re-fetch while crystallize is running" 230-247 and "Background crystallize gates" 249-254 are rare paths; "Cost gate" 150-163 overlaps `use-ai-integration` 225-230 and `skill-outline-fields.md` (the measured-estimate rule is stated in three places). | low |
| 5 | Menus without a default [AN "one default"] | 140-148 presentation "natural-language summary; pick a table / inline JSON / structured prose if ..."; 98-99 validation code "inline bash, `uv run --no-sync python -c`, or short scripts". Minor. | low |
| 6 | Principle cited by file name that has moved (144, 213 "per the preserve-and-surface principle (CLAUDE.md)") | The principle text lives in AGENTS.md / `harden-creation.md` 236. | low |
| 7 | Hard gates are prose, not a checklist [AN] | The present -> feedback -> updated-sample loop (133-138) and the two "explicit confirmation" gates have no `- [ ]` checklist form. | low |

##### github-sync (SKILL.md 238 lines, ~2,100 tokens, no references)

What it does well: numbered Enable 1-11 with exact commands, explicit hard rules (27-36), a precise permission list with per-scope rationale (72-84), the "rejected call is not a missing grant" gotcha (90-103), Repair and Disable sections.

| # | Rule | Evidence | Sev |
|---|---|---|---|
| 1 | Progressive disclosure: everything inline, no `references/` [AN] | The 26-line supervisord block (161-187), the TOML marker (140-144), and the latchkey permission mechanics (45-103, 59 lines) are copy material. The `[program:github-sync]` block is also described in `system/libs/github_sync/README.md` (11, 53): two places to maintain. | medium |
| 2 | Duplicated with `connect-external-service/references/latchkey.md` | 45-55 and 90-103 re-explain the one-request-per-call and "wait for the grant message" rules the latchkey reference owns (linked at 46). | medium |
| 3 | Time-sensitive / implementation-bound text [AN] | 19 "every 60s"; 174 the exact command line incl. the `oom_tag_service.py` wrapper and `uv sync --all-packages --frozen`; 205-206 log paths. A service change silently stales the skill. | medium |
| 4 | Description mixes what/when with policy and a restic parenthetical [AN] | line 3, 396 chars; the data/ caveat is repeated in the body at 23-25 and 196-198. | low |
| 5 | No validation step after the service is added [AN feedback loops] | Step 9 writes the block and runs `supervisorctl update`; nothing says to check `supervisorctl status github-sync` or run `check-app-errors` before step 10's commit, although AGENTS.md requires that after any service edit. | medium |
| 6 | `compatibility` used as a prose pointer | line 4. The spec reserves it for environment requirements; `spec-summary.md` says omit unless needed. | low |

##### data-pipeline-builder (SKILL.md 237 lines, ~3,400 tokens, no references)

What it does well: numbered steps with time budgets, explicit anti-patterns ("NEVER run a full-data inspection" 20-26), one storage default per access pattern (85-99), a merge rule that makes idempotency fall out (101-124), a test-first contract.

| # | Rule | Evidence | Sev |
|---|---|---|---|
| 1 | Concision: rule-heavy prose that over-specifies [AN "Claude is already very smart"; AS "concise, stepwise guidance with a working example outperforms exhaustive documentation"] | 11 `##` sections, zero code blocks, no worked example. Paragraphs of 15-20 lines carrying 6-10 rules each (41-58, 113-124, 170-190). ~3,400 tokens for a skill whose core is five rules (identity / version / merge / ledger / single writer). | medium |
| 2 | Progressive disclosure [AN] | Nothing split out: the step-1 profiling script spec (41-58), the 7b error contract (170-190), 7c retention (192-210), environment hygiene (232-237) are reference material. | medium |
| 3 | Conflicts with the repo's own skill packaging [REPO type-skill.md] | 69, 205, 217 put tests at `tests/test_parse.py`, `tests/test_pipeline.py`; `type-skill.md` 28-31 has to override this in prose. 146 "stdlib only" conflicts with the `python/pyproject.toml` dependency model. | medium |
| 4 | Voodoo constants [AN] | "at most 60 lines" (41), "at most eight rows" (73), "under ~2k records" (131), "`min(8, cpu_count)`" (133), "600 ... never more than ~800 ... `verify/` under ~150" (146-148), "more than 20%" (185), "20 random records" (222), "README at most 50 lines" (226), "~10% more" (14). None justified. | low |
| 5 | Step numbering 1-8 plus "7b", "7c" (170, 192) | Breaks "follow the steps in order" (12) and the three-write budget (15-16) that references steps by number. | low |
| 6 | Description | Third person, what+when, keywords. No offense. | - |

##### use-ai-integration (SKILL.md 235 lines, ~2,200 tokens; ref `billing-and-credentialing.md` 99)

What it does well: "the weakest scenario that does the job" with one default per scenario (69-84), code sketches per scenario, a measured-cost rule (225-230), a reference linked with a clear "when" (232-235).

| # | Rule | Evidence | Sev |
|---|---|---|---|
| 1 | Scripts are copied, not run or imported [AN scripts; REPO packaging] | 124 "copy this skill's `python/use_ai_integration_skill/claude_p.py` and call `claude_p_completion`", 152-153 same for `claude_p_task`, code comments 102, 129 "the file you copied in". Every consumer forks a 495-line file; `spec-summary.md` says a consumer adds `<name>-skill = { workspace = true }` and imports the package. Lines 34 and 45 already import from the package, so the file contradicts itself. | high |
| 2 | Progressive disclosure: setup detail before the decision [AN overview first] | 16-67 (52 lines) on credential resolution, snapshotting and secrets-guard rules precede "Pick the scenario" (69). The snapshot / retire / re-key procedure (37-57) is setup-once material for `references/`. | medium |
| 3 | Duplication between SKILL.md and the reference | Credential resolution order and the "never snapshot `CLAUDE_CODE_OAUTH_TOKEN`" rule at SKILL 21-31, 37-57 and `billing-and-credentialing.md` 43-82; the footgun at SKILL 234-235 and ref 93-99. | medium |
| 4 | Time-sensitive content [AN] | `billing-and-credentialing.md` 50-54 "This used to be one shared `~/.claude/settings.json` ... it is no longer where a CHAT's credential lives"; 26-31 a measured cost table ("~238k", "~$0.086") tied to a model and date; 84-91 "The mngr `claude -p` session-hook bug". History and a bug write-up, not instructions. | medium |
| 5 | Model ids hardcoded in examples [AN time-sensitive] | 65, 112, 134, 217 `claude-haiku-4-5`. | low |
| 6 | Description second person ("Use when writing or reasoning about code that calls Claude") | line 3; otherwise what+when with keywords. | low |
| 7 | Package install info [AN "list required packages"] | 96-97 says litellm is in the root `pyproject.toml`; nothing says how a consumer app with its own venv (per `post-crystallize-migration.md` 36) gets `litellm` or `use-ai-integration-skill`. | low |

##### heal-creation (SKILL.md 203 lines, ~1,840 tokens, no references of its own)

What it does well: a "When NOT to heal" section (34-40), numbered Steps 1-4, the same explicit substitutions pattern as update-creation, shortest of the trio.

| # | Rule | Evidence | Sev |
|---|---|---|---|
| 1 | Duplication with `update-creation` (cross-cutting 1) | 40 verbatim lines across 8 runs (23-25, 45-47, 56-58, 64-72, 75-78, 99-101, 136-140, 168-177); the go-live list 180-191 is a one-paragraph rewrite of update 249-263 with the same `host_backup` / `oom_priority` program names. | high |
| 2 | Eager loads / chains (cross-cutting 2) | `harden-contention.md` at 60 and 171 (405 lines, no TOC), `lead-proxy.md` at 156 (409), `worker-reporting.md` at 82 (236); the task file sends the worker through `shared/worker/SKILL.md` -> `harden-creation.md` (552) -> `op-heal.md`. | high |
| 3 | Dense go-live paragraph instead of a list [AN "clear steps"] | 178-192 is one 15-line sentence covering four creation types with nested parentheticals; update-creation 249-263 has the same content as bullets, so the two leads format the same rule differently. | medium |
| 4 | Critical-app warning is a hedge with no procedure [AN] | 27-32 "a live edit of the served tree at your own risk ... take [`update-app`'s careful flow] whenever the fix can wait for a preview": no decision rule for when it cannot wait, and no step that branches on it. | low |
| 5 | Description: imperative, second person ("after you worked around the failure") | line 3. | low |
| 6 | Ticket type differs from siblings without explanation | 74 `-t bug` vs update 107 `-t task`; one more divergence for a contention check that "matches on" the title (68). | low |

##### Summary table

| Skill | Lines | ~Tokens | Refs | Top offenses |
|---|---|---|---|---|
| manage-desktop | 327 | 4,800 | 0 | full manual inline (40% lookup tables), drift-prone "today"/"yet" statements, 2nd-person desc |
| agentic-browser-fleet | 303 | 3,800 | 0 | ~100 lines of conditional recovery/gotcha content inline, instruction-laden desc, internal repetition |
| update-creation | 282 | 2,700 | 0 (+1,330 shared) | 40 lines verbatim-shared with heal, 2-level ref chains, no TOCs, critical-app carve-outs inline |
| caretaker | 274 | 3,700 | 0 | 717-char desc, verbatim message/template assets inline, checks spelled out twice |
| crystallize-creation | 261 | 2,330 | 2 (96, 148) | lead-proxy/worker chains, 56-line heredoc inline, three entry modes braided without a branch map |
| fetch-process-show | 254 | 2,800 | 0 (+143 shared) | unreadable without `interactive-delivery.md`, two-level chain to the poll command, 526-char desc |
| github-sync | 238 | 2,100 | 0 | supervisord block and latchkey mechanics duplicated from README / latchkey.md, 60s and paths baked in, no post-install check |
| data-pipeline-builder | 237 | 3,400 | 0 | exhaustive rule prose with no example, test layout contradicts `type-skill.md`, nine unexplained constants |
| use-ai-integration | 235 | 2,200 | 1 (99) | "copy this file" instead of import/run, setup detail before the decision, history and cost tables in the ref |
| heal-creation | 203 | 1,840 | 0 (+1,250 shared) | 40 lines verbatim-shared with update, same chains/TOC gaps, 15-line go-live sentence |

Out of scope but noticed: the shared references these skills lean on (`lead-proxy.md` 409, `harden-contention.md` 405, `harden-creation.md` 552, `worker-reporting.md` 236) are the real progressive-disclosure problem for the lifecycle family; all four exceed the 100-line TOC threshold with no TOC and link onward to each other.

### Appendix E: imbue-studio, datalib, launch-task, manage-routines, create-theme, file-sharing, find-transcripts, check-app-errors, submit-upstream-changes, make-theme-icon, use-screen, assist

#### dwt skills audit: small/medium batch (12 skills)

Scope: `imbue-studio`, `datalib`, `launch-task`, `manage-routines`, `create-theme`, `file-sharing`, `find-transcripts`, `check-app-errors`, `submit-upstream-changes`, `make-theme-icon`, `use-screen`, `assist`. All paths relative to `.agents/skills/`. Sizes are SKILL.md body (after frontmatter); tokens estimated as words x 1.35. None of these breach the 500-line / 5,000-token limits, so the offenses here are about disclosure, duplication, content hygiene, and description quality rather than raw size.

| skill | body lines | ~tokens | desc chars | refs | severity summary |
|---|---|---|---|---|---|
| imbue-studio (upstream-owned) | 211 | 2.9k | 466 | 0 | medium: full manual inline, permission boilerplate duplicated |
| datalib | 209 | 2.3k | 357 | 0 (+1 script) | medium: version pin in prose, reference material inside numbered steps |
| launch-task | 206 | 2.0k | 162 | 2 (+shared) | high: core workflow lives in 409-line shared ref; 2-3 level chains |
| manage-routines | 154 | 1.6k | 449 | 0 | low: internals inline, tz repair in prose |
| create-theme | 154 | 2.0k | 473 | 0 | medium: eager read of blueprint plan; contract detail inline |
| file-sharing | 149 | 2.0k | 161 | 0 | medium: description promises Notes/Photos the body lacks; workflow split into "Examples" |
| find-transcripts | 127 | 1.2k | 384 | 0 | medium: legacy-format handling inline; 15-line jq in prose |
| check-app-errors | 117 | 1.1k | 343 | 0 | low-medium: deterministic greps not scripted; incident anecdote |
| submit-upstream-changes | 112 | 1.0k | 445 | 1 | medium: no when-clause; contradictory config-vs-hardcoded; 3x repeated snippet |
| make-theme-icon | 112 | 1.2k | 566 | 0 | low-medium: longest description in batch, body text in description |
| use-screen | 98 | 1.4k | 449 | 0 | low-medium: lease semantics triplicated across skills |
| assist | 95 | 1.8k | 273 | 0 | low-medium: dense paragraphs, deterministic classification unscripted |

Shared targets these skills load (outside `.agents/skills/`), measured with `wc -l`:

| file | lines | TOC? | loaded by |
|---|---|---|---|
| `.agents/shared/references/lead-proxy.md` | 409 | no (9 H2s) | launch-task step 4 (core flow) |
| `.agents/shared/references/worker-reporting.md` | 236 | no | launch-task task template (worker side) |
| `.agents/shared/references/harden-contention.md` | 405 | no | launch-task/references/worker-failure.md |
| `docs/system/blueprint/workspace-themes/plan-workspace-themes.md` | 364 | numbered sections | create-theme ("read sections 3 and 4", ~170 lines), make-theme-icon (section 6, ~44 lines) |
| `docs/system/themeable-apps.md` | 122 | - | create-theme step 6 |
| `system/libs/routines/README.md` | 107 | - | manage-routines ("Full reference") |

Rules cited: **A** = Anthropic best-practices page; **S** = agentskills.io spec/best-practices; **C** = Academy course; **R** = repo's own `spec-summary.md` / `validate_skill.py` / AGENTS.md.

---

##### imbue-studio (vendored from mngr via `system/scripts/vendor_mngr_skill.py`; fix upstream, not here)

Does well: decide-transport-first branching (L18-37), "read the live contract" instead of memorized routes (L10-16, L83-92), compact permission table (L102-113), explicit "Never" list.

| # | offense | rule | evidence | sev |
|---|---|---|---|---|
| 1 | SKILL.md is the whole manual, no references/: route recipes for create/SSH/export/migrate (section 6, L130-173, 44 lines) and the shared-workspace trust model (section 7, L175-197, 23 lines) are conditional detail loaded on every activation | A progressive disclosure ("move advanced features, API reference, long workflows to separate files"); S resources tier | `imbue-studio/SKILL.md:130-197` | medium |
| 2 | Permission-request mechanics ("exactly one request per tool call, output untouched, end your turn") restated here, in `file-sharing/SKILL.md:118-124`, and in `connect-external-service/references/latchkey.md` ("File exactly one permission request per tool call") | A consistent single source; S "coherent units" | `imbue-studio/SKILL.md:116-128` | medium |
| 3 | Description is a table of contents ("Explains how to reach..., how to start..., where the live contract lives, which permissions..., how to tell whether..., and what an agent must never do"), 466 chars; trigger terms are only the first sentence | A "description = what it does + when to use it", keep specific keywords; S "a few sentences" | `imbue-studio/SKILL.md:3` | low-medium |
| 4 | Time-sensitive migration note about `~/.minds` data blocking the source-checkout launch | A "avoid time-sensitive information" | `imbue-studio/SKILL.md:73` | low |
| 5 | Launch is a menu of four invocations (launch.argv / `open -g -n -a` / `imbue-studio --background` / AppImage / `uv run minds run`) | A "provide a default, not a menu" (partly mitigated: each is conditional on platform) | `imbue-studio/SKILL.md:70-73` | low |
| 6 | Terminology drift: "Imbue Studio", "the `minds` app", "studio API", "the hub" (L149), "desktop UI" | A consistent terminology | `imbue-studio/SKILL.md:10,149` | low |

##### datalib

Does well: `compatibility` frontmatter used correctly (259 chars); install script is *run*, idempotent, self-documenting; defers everything volatile to `datalib-step help` (L124-146) instead of restating CLI docs; strong negative trigger in the description.

| # | offense | rule | evidence | sev |
|---|---|---|---|---|
| 1 | Version pin written into prose three times (`v0.49.0` in a comment, a docs path, a GitHub URL). The script owns `PINNED_VERSION` and a test enforces the prose copy matches, i.e. the duplication is institutionalized rather than removed | A "avoid time-sensitive information"; A "consistent single source" | `datalib/SKILL.md:40,151,159`; `datalib/scripts/install_datalib.sh:20-22` | medium |
| 2 | "## Instructions" is one 7-item numbered list (L49-122) mixing procedure (1,2), policy (3,4), and reference (6: port + token path; 7: backup-exclusion edge case with `backup.toml` semantics). Items 6-7 (L104-122) are reference-grade | A "SKILL.md = overview + core workflow"; S "concise, stepwise guidance" | `datalib/SKILL.md:49-122` | medium |
| 3 | "Supported sources" incl. Cloudflare TLS-fingerprint / desktop-proxy internals (33 lines) is conditional troubleshooting detail inline | A progressive disclosure (conditional details -> separate file) | `datalib/SKILL.md:176-208` | medium |
| 4 | Two-level cross-skill reference: points at another skill's reference file and a named subsection of it | A "keep references one level deep from SKILL.md" | `datalib/SKILL.md:168-169, 198-199` -> `connect-external-service/references/latchkey.md` | low-medium |
| 5 | Hard-coded service port `127.0.0.1:8731` and token path in prose, while a helper (`sync_request.py`) already exists that could expose `health`/`token` | A "scripts solve, don't defer"; A no voodoo constants in prose | `datalib/SKILL.md:104-112` | low |
| 6 | Heading "## Instructions" is a generic label; the body opens with a 12-line policy paragraph + 13-line env setup before step 1 | A "name sections descriptively"; S concision | `datalib/SKILL.md:11-47` | low |

##### launch-task

Does well: numbered steps 1-4, scripts are run (never read), failure/recovery split into conditional references, explicit "the report file is the contract" guidance.

| # | offense | rule | evidence | sev |
|---|---|---|---|---|
| 1 | The core workflow (parse report, decide gate vs escalate, merge, timeout handling) is not in SKILL.md: step 4 is "Follow `.agents/shared/references/lead-proxy.md`" (409 lines, no TOC) and step 1's task template sends the worker to `worker-reporting.md` (236 lines, no TOC). Effective lead-side load = 206 + 409 = 615 lines; a worker reading its brief adds 236. SKILL.md then carries a 19-line "substitutions" table (L167-185) to parametrize that generic reference, which is a symptom of the reference being too abstract for this caller | A "SKILL.md keeps the core workflow"; A "TOC for reference files longer than 100 lines"; S "<5000 tokens on activation" (effective load ~8k) | `launch-task/SKILL.md:61-67,157-185`; `.agents/shared/references/lead-proxy.md` (409), `worker-reporting.md` (236) | high |
| 2 | Reference chains 2-3 deep: SKILL.md -> `references/worker-failure.md` -> `lead-proxy.md`, `harden-contention.md` (405 lines); SKILL.md -> `references/dead-worker-recovery.md` -> `.agents/shared/references/freeing-memory.md`; `worker-failure.md` -> `dead-worker-recovery.md` | A "all reference files should link directly from SKILL.md; avoid nested chains"; S same | `launch-task/references/worker-failure.md:12,26,51,53`; `dead-worker-recovery.md:18` | medium |
| 3 | Duplicated content: `source_artifacts_dir` explained twice (L114-116, L208-210); dead-worker restart rules in Guidelines (L199-204) restate `dead-worker-recovery.md:3-5,25-37`; `worker-failure.md:11-13,22-29` restate SKILL.md L195-198; `tk` step etiquette (L15-31, 17 lines) restates AGENTS.md L68 | A concision / single source | `launch-task/SKILL.md:15-31,114-116,199-210` | medium |
| 4 | Stale/contradictory instruction inside a reference: "finalize via `submit-upstream-changes` when done" and "push to `submit/<name>` per the submit-upstream-changes skill" conflict with SKILL.md's terminal flow (`done` -> merge the worker's branch, L183-185) and with submit-upstream-changes' own purpose (template PRs) | A consistent terminology/flow; S refine with real execution | `launch-task/references/dead-worker-recovery.md:37,73` | medium |
| 5 | Harden-flow jargon leaks into the generic skill: "Gate 2 proposal" (undefined here), example path `data/.tasks/harden/<slug>/reports/report.md` | A "assume Claude lacks only your domain context"; consistent terminology | `launch-task/references/worker-failure.md:20-23` | low |
| 6 | Three names for one thing: "sub-agent", "worker", "background agent" (the skill itself legislates the user-facing word at L17 then uses the others) | A consistent terminology | `launch-task/SKILL.md:17,25,27,30,89` | low |
| 7 | Description (162 chars) has weak trigger vocabulary: no "delegate", "background agent", "worker", "in the background", "report", "gate" | A/S specific keywords in description | `launch-task/SKILL.md:3` | low |
| 8 | Constants in prose: "30m timeout" (L163, L195), exit code 75 (L138), presumably also encoded in `create_worker.py` | A no voodoo constants / single source | `launch-task/SKILL.md:138,163,195` | low |
| 9 | Dense 10-line `--branch` paragraph that largely restates the flag's `--help` (which it also tells you to read) | A concision | `launch-task/SKILL.md:118-127` | low |

##### manage-routines

Does well: best description in the batch (what + concrete trigger phrases + scope note); one command surface with worked examples; points to the library README (107 lines) as the full reference instead of inlining it.

| # | offense | rule | evidence | sev |
|---|---|---|---|---|
| 1 | Timezone repair is a 3-command system mutation (`ln -sf /etc/localtime`, write `/etc/timezone`, write `user_timezone`) given as prose to re-derive each time | A "scripts solve problems rather than defer to Claude"; R `[script]` for deterministic steps | `manage-routines/SKILL.md:28-48` | medium |
| 2 | "How it runs" internals (cron tick cadence, lock semantics, ledger paths, catch-up policy) inline, 16 lines; reference-grade | A progressive disclosure | `manage-routines/SKILL.md:134-149` | low-medium |
| 3 | Flag catalogue paragraph (L73-80) restates `routines add --help` | A concision ("does Claude need this?") | `manage-routines/SKILL.md:73-80` | low |
| 4 | Caretaker section duplicates `caretaker` skill / AGENTS.md L204 and carries the time-sensitive "it is a beta feature" | A avoid time-sensitive info; single source | `manage-routines/SKILL.md:151-160` | low |
| 5 | Dated example `--once 2026-11-05T09:00` | A avoid time-sensitive info (cosmetic) | `manage-routines/SKILL.md:70` | low |

##### create-theme

Does well: 7 numbered steps; validate-until-clean loop (step 4); visual judge loop (step 5); clean hand-off to `make-theme-icon`; "ask nothing more for a named real system" default.

| # | offense | rule | evidence | sev |
|---|---|---|---|---|
| 1 | Eager external load of a design plan: "read sections 3 and 4 before writing any CSS" of `docs/system/blueprint/workspace-themes/plan-workspace-themes.md` (364 lines; sections 3-4 = L30-201, ~170 lines). The contract the skill depends on lives in a blueprint doc, not a curated skill reference; effective load ~154 + 170 (+122 for `themeable-apps.md` at step 6) | A "bundle reference material in the skill, link one level deep"; S resources tier; A "organize for discovery" | `create-theme/SKILL.md:14-16,112` | medium |
| 2 | Step 3 "Write it" is 30 lines of contract detail (allowed selectors, token rules, app overlays, CSS-library vendoring with a node command) in one bulleted block | A move "domain-specific schemas / API reference" to references; A keep paragraphs short | `create-theme/SKILL.md:48-77` | medium |
| 3 | Step 6 scripts the user-facing message in 37 lines and installs behaviour by appending a verbatim line to `AGENTS.md` ("That line is the only thing that ever tells app building about a theme") -- config via prose side-channel | A templates belong in a reference/asset; R project-config over hidden conventions | `create-theme/SKILL.md:100-136` | low-medium |
| 4 | Description (473 chars) is a procedure summary ("Lays out..., writes..., checks..., judges..., lets..., asks...") after the trigger sentence | A description = what + when, not the workflow | `create-theme/SKILL.md:3` | low |
| 5 | No `references/` at all for a skill whose contract is external and 364 lines long | A progressive disclosure | `create-theme/` | low |

##### file-sharing

Does well: step list with error-code meanings (502/503); real `latchkey curl` examples; synced-folder write/revert semantics made explicit; permission payload schema spelled out.

| # | offense | rule | evidence | sev |
|---|---|---|---|---|
| 1 | Description promises "importing Apple Notes, Photos, and other similar locally stored data" and the body contains no procedure for either (no mention of Notes/Photos anywhere in L8-155) | A description accuracy; C "description is the matching key" | `file-sharing/SKILL.md:3` vs body | medium |
| 2 | Description (161 chars) is imperative ("Use to read and write...") with few trigger words (no "my computer", "local disk", "Downloads", "sync a folder"); the trigger sentence lives in the body instead (L12) | A third-person what+when; keywords | `file-sharing/SKILL.md:3,12` | low-medium |
| 3 | Core workflow split: the numbered list (L20-25) forward-references "the ... example below" twice, and the real procedure (permission payload schema L126-135, sync request L137-150) sits under an "Examples" appendix | A "workflows have clear steps"; S stepwise guidance | `file-sharing/SKILL.md:20-27,90-150` | medium |
| 4 | Permission-request boilerplate (one request per tool call, never pipe through jq, body fields) duplicated with `imbue-studio/SKILL.md:116-128` and `connect-external-service/references/latchkey.md` | single source | `file-sharing/SKILL.md:113-135` | medium |
| 5 | Synced-folder semantics (L30-88, 59 lines: inactive dir, write reversion, .git, symlinks, deletion) is reference-grade; SKILL.md needs the 5-line rule, not the manual | A progressive disclosure | `file-sharing/SKILL.md:30-88` | medium |
| 6 | Compat note for older Imbue Studio builds (permission names without `<device ID>:`) inline | A time-sensitive info -> "old patterns" section | `file-sharing/SKILL.md:53-55` | low |
| 7 | Assumes `xmlstarlet` is installed; no availability/install note | A "don't assume packages are installed" | `file-sharing/SKILL.md:110` | low |
| 8 | Terminology drift: "Latchkey Gateway", "latchkey", "file server", "WebDAV server", "remote WebDAV server"; sample paths use a personal name (`/home/hynek/...`) | A consistent terminology; concrete-but-neutral examples | `file-sharing/SKILL.md:22,27,100-144` | low |
| 9 | Typo "possible even other OSes" | - | `file-sharing/SKILL.md:154` | low |

##### find-transcripts

Does well: short; opens with the one behavioural rule that matters ("don't refuse before you check"); two concrete discovery commands; renderer handles both formats.

| # | offense | rule | evidence | sev |
|---|---|---|---|---|
| 1 | Legacy format handling inline: "retired pre-ATIF format" (L96-98), dual-format jq branches for `user_message`/`assistant_message`/`tool_result` (L115-119), old-format truncation caveats (L123-127) | A time-sensitive/legacy content -> "old patterns" section or reference | `find-transcripts/SKILL.md:96-127` | medium |
| 2 | 15-line jq program embedded in prose for the agent to retype | A "bundle scripts for repeated logic"; S "bundle scripts when the agent keeps reinventing the same logic" | `find-transcripts/SKILL.md:105-121` | medium |
| 3 | `/home/user/.mngr` hard-coded 8 times with a one-line "use `$MNGR_HOST_DIR` if elsewhere" aside | A single default, no scattered constants | `find-transcripts/SKILL.md:21,25,27,59,70,74,79,89` | low |
| 4 | "What this skill does NOT cover" (L52-64) interrupts the flow between setup and step 1; "One chat, several agents" chat-app internals (L35-50, 16 lines) could be a reference | A overview first, details later | `find-transcripts/SKILL.md:35-64` | low |
| 5 | Description ends with an inline "NOTE:" scope disclaimer and an imperative "Use this skill any time..." | A description style | `find-transcripts/SKILL.md:3` | low |

##### check-app-errors

Does well: broad-then-narrow strategy stated once (L15-18); steps 1-3; "report where you found nothing" guidance; notes on rotation and non-persistence.

| # | offense | rule | evidence | sev |
|---|---|---|---|---|
| 1 | The deterministic core (two greps + `ls -lt` + `tail -n 5` + `routines list`) is prose for the agent to re-run each time, although L15-18 stresses cost because the Caretaker runs it routinely | A "scripts solve, don't defer"; S bundle scripts for repeated logic; R `[script]` | `check-app-errors/SKILL.md:44,57,64-67,86-88` | medium |
| 2 | Incident anecdote ("one workspace served eight-day-old data through a quarter of a million such lines") as justification | A concision ("does this paragraph justify its token cost"); R comment hygiene | `check-app-errors/SKILL.md:49-55` | low-medium |
| 3 | Cross-reference by step number into a 540-line skill ("see step 2 of the `update-app` skill") -- brittle | A "name files/sections descriptively" | `check-app-errors/SKILL.md:121-123` | low |
| 4 | "Step 2b" inserted step | structure | `check-app-errors/SKILL.md:84` | low |
| 5 | Description names an internal consumer ("The Caretaker agent reuses this as its log-scan step") -- not a trigger | A description = what + when | `check-app-errors/SKILL.md:3` | low |

##### submit-upstream-changes

Does well: short; PR conventions explicit; mngr changes split into a reference; worktree vs in-place path signposted on a stated condition (dirty tree).

| # | offense | rule | evidence | sev |
|---|---|---|---|---|
| 1 | Description (445 chars) has no when-clause; it is policy ("Opens a separate per-feature PR...; never pushes directly to upstream main. Do not push agent-specific content"). The "When to push" lives in the body (L106-109) | A/S description must say when to use it | `submit-upstream-changes/SKILL.md:3,106-109` | medium |
| 2 | Contradictory config source: "upstream URL and base branch are in `system/config/parent.toml`" (L43, L93) but step 3 hard-codes `--repo imbue-ai/default-workspace-template --base main` | A consistency; one default | `submit-upstream-changes/SKILL.md:43,93,96-98` | medium |
| 3 | Same 5-line `python3 -c tomllib` snippet pasted three times, and two near-identical 12-line git blocks; a fragile fixed sequence (fetch, worktree, cherry-pick, push, cleanup) left as prose | A "low freedom = exact script"; A scripts for fragile sequences | `submit-upstream-changes/SKILL.md:48-52,61-65,79-83,59-73,77-89` | medium |
| 4 | "**Co-Authored-By trailer** on the commit (the standard one used in this repo)" -- names no trailer, and conflicts with the repo owner's standing rule against attribution trailers | stale/inaccurate content | `submit-upstream-changes/SKILL.md:39` | medium |
| 5 | "CLAUDE.md scaffolding" (L3, L18, L27) while the template's instructions file is `AGENTS.md` (CLAUDE.md is a 7-line shim) | consistent terminology / outdated | `submit-upstream-changes/SKILL.md:3,18,27` | low-medium |
| 6 | Repetition: "never push to upstream main" x3 (desc, L10, L117); "one logical fix per PR" x3 (L10, L36, L115); "use update-self to pull" x2 (desc, L118); "## Important" is a restatement list | A concision | `submit-upstream-changes/SKILL.md:10,36,111-118` | low |
| 7 | `references/mngr-changes.md` is mostly developer-side `just` recipes that it itself says "none of that is something an agent in a workspace does" | A add what the agent lacks, omit the rest | `submit-upstream-changes/references/mngr-changes.md:17-27` | low |

##### make-theme-icon

Does well: 5 numbered steps with a fit -> look -> redraw loop; a default path (image tool) plus a worked escape hatch (text grid) with a concrete example; install/check commands exact.

| # | offense | rule | evidence | sev |
|---|---|---|---|---|
| 1 | Description is 566 chars (longest in batch): five trigger situations (good) followed by two sentences of capability/body content ("Works with an image tool the harness gives, or with none: the agent can draw a pixel grid or an SVG itself, and the workspace-themes tools fit any drawing...") | A description = what + when, keep it tight; S "a few sentences" | `make-theme-icon/SKILL.md:3` | medium |
| 2 | Eager loads: "Read the guide in full" (theme's `icons/guide.md`, size per theme) plus the design plan section 6 (`plan-workspace-themes.md:256-299`) as "the full design" | A link reference material from the skill, one level; design docs are not curated references | `make-theme-icon/SKILL.md:14-15,36` | low-medium |
| 3 | Conditional detail inline: background-worker `--repo-root` rules (L100-106), built-in-theme refusal rules (L108-111) | A conditional details -> reference | `make-theme-icon/SKILL.md:100-111` | low |
| 4 | "app" overloaded: `--app <registered app name>`, `--app app` (generic icon), "an app", "the app's own icon" | A consistent terminology | `make-theme-icon/SKILL.md:27,91,96-98` | low |

##### use-screen

Does well: one command block covering every verb; lease rules with exact exit codes; input path chosen by target with a default each.

| # | offense | rule | evidence | sev |
|---|---|---|---|---|
| 1 | Lease semantics ("human always wins", acquire/handoff/idle) are documented here (L38-63), in `agentic-browser-fleet/SKILL.md` (~L215 on), and the browser lifecycle again in `manage-desktop/SKILL.md:289` -- one concept, three places | single source; S coherent units | `use-screen/SKILL.md:38-63` | medium |
| 2 | Description (449 chars) is second person ("Use when you need a display... you drive it") and ends with an explanatory sentence that belongs in the body ("The Screen is the workspace's one streamed Linux desktop...") | A third person; what+when only | `use-screen/SKILL.md:3` | low-medium |
| 3 | "What to know" internals (memory bands, ten-second re-banding, Chromium window-class names `tilion`/`chrome`/`fortress`) inline | A progressive disclosure | `use-screen/SKILL.md:94-104` | low |
| 4 | Verb table (L21-32) duplicates `screen --help`; service URL `127.0.0.1:8090` and display `:99` repeated in prose | A concision; constants in one place | `use-screen/SKILL.md:16-32,71-74` | low |

##### assist

Does well: six ordered steps; hard "confirm before touching anything" gate (step 4); evidence-vs-hypothesis discipline; decision table.

| # | offense | rule | evidence | sev |
|---|---|---|---|---|
| 1 | Step 3A origin classification (vendor path, root commit, `update-self:` merge ancestry) is a deterministic git procedure left as prose + three loose commands | A "scripts solve, don't defer"; R `[script]` for deterministic steps | `assist/SKILL.md:35-52` | medium |
| 2 | Wall-of-text bullets: L27 (~90 words, one bullet), L59-61, L78 (~110 words), L79 | A concise, short paragraphs; "does this paragraph justify its token cost" | `assist/SKILL.md:27,59-61,78-79` | low-medium |
| 3 | Decision table (L90-101) restates steps 3-6; step 5's critical-app bullet restates `update-app`'s careful-flow pitch | A concision / single source (table is useful, the prose above it is then redundant) | `assist/SKILL.md:78,90-101` | low |
| 4 | Two-level cross-skill reference to another skill's 492-line reference (`update-app` -> `references/critical-app.md`) | A one level deep | `assist/SKILL.md:78` | low |
| 5 | Terminology drift: "imbue" / "Imbue Studio" / "upstream"; "built-in" / "template built-in" / "mngr" / "outer app" | A consistent terminology | `assist/SKILL.md:10,37,58-61,84-88` | low |

---

##### Cross-cutting findings for this batch

1. **Shared references used as core-workflow homes have no TOC and are 236-409 lines** (`lead-proxy.md`, `worker-reporting.md`, `harden-contention.md`). Every skill that says "follow lead-proxy.md" (here: launch-task) inherits an effective activation load well past the 5,000-token guideline and an un-navigable file (A: TOC for >100 lines; A: "if Claude reads a file repeatedly, that content belongs in SKILL.md").
2. **Design/blueprint documents serve as the contract reference** for the theme skills (`docs/system/blueprint/workspace-themes/plan-workspace-themes.md`, 364 lines) rather than a curated `references/` file inside the skill. Plans are history-shaped, not agent-shaped, and live outside the skill directory.
3. **Permission-request boilerplate is written out in at least three places** (imbue-studio, file-sharing, latchkey.md), with slight wording differences each time.
4. **Deterministic procedures left as prose** in six of twelve skills (manage-routines timezone repair, find-transcripts jq renderer, check-app-errors log scan, submit-upstream-changes cherry-pick/push sequence, assist origin classification, datalib health/token). The repo's own `spec-summary.md` classifies these as `[script]` steps.
5. **Descriptions**: four are second person or imperative-first ("Use when you...", "Use to..."), one has no when-clause (submit-upstream-changes), one over-promises (file-sharing: Apple Notes/Photos), two carry body content (make-theme-icon, use-screen), one is a table of contents (imbue-studio). None exceed 1,024 chars; the range is 161-566.
6. **Legacy/time-bound text inline** rather than an "old patterns" section: find-transcripts (pre-ATIF format), datalib (v0.49.0 x3), file-sharing (pre-per-device permission names), imbue-studio (`~/.minds` move), manage-routines ("beta feature").
7. **Out of scope but noticed**: `launch-task/references/dead-worker-recovery.md:37,73` directs finished workers to `submit-upstream-changes`, which contradicts launch-task's own `done -> merge` flow; `submit-upstream-changes/SKILL.md:39` prescribes a Co-Authored-By trailer. Both read as stale content rather than structure problems.

### Appendix F: blueprint, blueprint-generate, show-files-in-chat, launch-custom-agent, connect-external-service, notify-user, do-something-new, use-from-phone, devices, handoff-summary, dealing-with-the-unexpected

#### dwt skills audit, batch: the 11 smallest skills

Scope: `blueprint`, `blueprint-generate`, `show-files-in-chat`, `launch-custom-agent`, `connect-external-service` (+3 references), `notify-user`, `do-something-new`, `use-from-phone`, `devices`, `handoff-summary`, `dealing-with-the-unexpected`. Every SKILL.md and reference file was read in full; `python/` dirs were skimmed only for their entry-point docstrings.

Rule sources cited: **[A]** platform.claude.com best-practices, **[AC]** academy.claude.com course, **[AS]** agentskills.io spec + best-practices, **[R]** the repo's own `.agents/shared/references/spec-summary.md` / `validate_skill.py`.

Headline for this batch: none of these 11 exceed 500 lines (largest body is 83 lines), and all pass the repo's `validate_skill.py` static checks. The offenses here are of a different kind than the giant skills: duplicated and orphaned reference files, descriptions that carry instructions instead of triggers, second-person voice, stale or dead content, unconditional "read X first" loading of a shared reference, and two skills whose body teaches nothing the agent does not already know or that belongs elsewhere.

---

##### blueprint (SKILL.md 89 lines, 3 refs)

Does well: numbered steps, clear "do NOT" gates, references are small and linked with explicit `[text](path)` links from SKILL.md.

| # | Offense | Rule | Evidence | Severity |
|---|---|---|---|---|
| 1 | Description has no when-clause and no trigger keywords. "Start a new plan-writing session. Explores the codebase and asks clarifying questions." says what, not when; nothing matches a user saying "write me a plan", "spec this out", "design doc". | [A] "include both what the Skill does and when to use it"; [AS] "should include specific keywords" | `blueprint/SKILL.md:3` | medium |
| 2 | Description is 86 chars, the second shortest in the collection; the collection's median is 370. Two sentences in different voices (noun phrase, then third-person verb). | [A]/[AS] description quality | `blueprint/SKILL.md:3` | low |
| 3 | Progress-line glyphs `✓ ● ○` are non-ASCII symbols baked into instructions. Repo convention (AGENTS.md / CLAUDE.md) forbids emoji; these are not emoji but are the same class of decorative glyph and the only ones in the whole skills tree. | repo "never use emojis"; [A] consistent plain output | `blueprint/SKILL.md:83-86` | low |
| 4 | Two reference files are byte-identical copies of files in `blueprint-generate` (`references/refine-prompt.md`, `references/templates.json`), and `references/questions.md` is a near-copy of `blueprint-generate/references/plan-questions.md` (identical body, different H1). Two sources of truth for the plan templates and the question format. | [A] "Keep references one level deep ... link directly"; general single-source-of-truth; repo spec-summary's `.agents/shared/` exists exactly for shared content | `blueprint/references/*` vs `blueprint-generate/references/*` | high |
| 5 | H1 is `## Blueprint — Start plan session` (an H2 used as the title, with an em dash); every other skill opens with `# <gerund phrase>`. Inconsistent with the collection. | [A] consistency within a skill collection | `blueprint/SKILL.md:8` | low |
| 6 | Two-skill design for one flow: Q&A is `blueprint`, generation is `blueprint-generate`; the second exists only because the user must type `/blueprint-generate` to end Q&A. The pair duplicates its references rather than one skill routing to `references/generate.md`. | [AS] "Design coherent units ... not too narrow (forces many skills to load)" | both SKILL.md files | medium |
| 7 | Skill is not referenced anywhere outside itself (no AGENTS.md mention, no other skill routes to it), so discovery relies entirely on the weak description in #1. | [AC] "If a skill doesn't trigger, add keywords" | grep for `blueprint` in AGENTS.md yields only the `docs/system/blueprint/` path | low |
| 8 | Output path convention `docs/system/blueprint/<slug>` is in `blueprint-generate` only; `blueprint` never says where the plan will land, so a user asking "where will it go" mid-Q&A is unanswerable from this file. | [A] workflows have clear steps | `blueprint-generate/SKILL.md:18,23`, absent from `blueprint/SKILL.md` | low |

##### blueprint-generate (SKILL.md 44 lines, 5 refs)

Does well: short, every step is one action, links use `[text](path)` markdown.

| # | Offense | Rule | Evidence | Severity |
|---|---|---|---|---|
| 1 | Orphan + nested chain: `references/plan-questions.md` is never linked from SKILL.md; it is reached only via `references/refinement.md:19`. This is the one two-level reference chain in the batch. | [A] "All reference files should link directly from SKILL.md"; [AS] "Avoid deeply nested reference chains" | `blueprint-generate/SKILL.md` (no mention), `references/refinement.md:19` | medium |
| 2 | Duplicated files: `refine-prompt.md` and `templates.json` are byte-identical to `blueprint/references/`; `plan-questions.md` duplicates `blueprint/references/questions.md`. | single source of truth; [A] one-level references from one place | see above | high |
| 3 | Over-fragmentation: `write-plan.md` is 12 lines and `refine-prompt.md` is 9 lines, each loaded by a separate file read. SKILL.md itself is 44 lines; inlining these would still leave it under 70 lines. Progressive disclosure is meant to keep SKILL.md short, not to split a short file into reads. | [A] "Does this paragraph justify its token cost?"; [AS] "Keep individual reference files focused" (focused, not atomised) | `references/write-plan.md`, `references/refine-prompt.md` | medium |
| 4 | Step 1 says "Recall the template name from the blueprint Q&A conversation" with no fallback when the conversation was compacted or the skill is invoked cold. | [A] "Solve, don't defer"; handle error conditions | `blueprint-generate/SKILL.md:14` | low |
| 5 | Description is 99 chars with a weak when-clause ("Use after the blueprint skill has gathered enough context") and no keywords for a user who says "ok write the plan now". | [A]/[AS] description keywords | `blueprint-generate/SKILL.md:3` | medium |
| 6 | Same non-ASCII progress glyphs as `blueprint`. | repo no-emoji rule | `SKILL.md:37`, `references/refinement.md:26` | low |
| 7 | Title is an H2 `## Blueprint — Generate plan`, not an H1. | collection consistency | `blueprint-generate/SKILL.md:8` | low |
| 8 | Name `blueprint-generate` is a noun+verb compound unlike any other name in the collection (the rest are verb-first: `build-app`, `update-self`, or plain nouns: `devices`, `datalib`). | [A] naming consistency across a collection | directory name | low |

##### show-files-in-chat (SKILL.md 86 lines)

Does well: concrete input/output examples, explains the one non-obvious rule (absolute path doubles as URL), names the supported formats, has a troubleshooting note.

| # | Offense | Rule | Evidence | Severity |
|---|---|---|---|---|
| 1 | Description is second person ("you have a file on disk you want the user to see"). | [A] "Always write in third person" | `show-files-in-chat/SKILL.md:3` | low |
| 2 | Repeats itself: the absolute-path requirement is stated at lines 33-40, again at 48, and a third time under Notes at 81-83; the `data/` placement rule at 19-24 is restated at 46-47. | [A] "Concise is key" | lines 33-40, 46-48, 81-86 | low |
| 3 | AGENTS.md already carries the core rule (link by absolute path, opens in Files, `show-files-in-chat` skill) at AGENTS.md:109. The skill restates it rather than adding only what AGENTS.md lacks. Partially justified: the inline-image rules and immutable caching note are new. | [A] "Only add context Claude does not already have" | AGENTS.md:109 vs SKILL.md:10-15, 44-58 | low |

##### launch-custom-agent (SKILL.md 80 lines, python entry point)

Does well: three numbered steps, one command with the required flags, script is told to be run (not read), failure modes of the script listed, mode choice given as two options without a hidden default.

| # | Offense | Rule | Evidence | Severity |
|---|---|---|---|---|
| 1 | Script invocation is a long module path (`uv run --no-sync .agents/skills/launch-custom-agent/python/launch_custom_agent_skill/launch_custom_agent.py`) rather than the spec's `run.py` entry; the repo's own cheat sheet says the entry is `python/<pkg>/run.py`. Inconsistent with sibling skills (`notify-user` uses `python3 .../notify_user.py`, `launch-task` has `scripts/create_worker.py`). | [R] spec-summary "The entry point, run.py"; [A] consistent terminology/paths across a collection | `launch-custom-agent/SKILL.md:40` | low |
| 2 | Implementation internals leak into the Guidelines: the `MNGR__ALLOW_SETTINGS_KEY_ASSIGNMENT_NARROWING=true` explanation (lines 74-77) and the claude folder-trust seeding (78-80) are facts about what the script already does, duplicated from the script's docstring; the agent needs neither to use the skill. | [A] "Scripts solve problems rather than defer"; "Does Claude really need this explanation?" | `SKILL.md:74-80` vs `launch_custom_agent.py:1-24` docstring | medium |
| 3 | The expected-but-harmless warning text ("WARNING: No API credentials detected for Claude Code") is explained in prose instead of being suppressed or re-explained by the script's own output. | [A] "Solve, don't defer" (handle error conditions in scripts) | `SKILL.md:61-63` | low |
| 4 | Description is long (413 chars) and half of it is a feature list ("bills through a signed-in provider account, and can notify the user") rather than triggers. | [A] "specific and includes key terms"; [AS] "A few sentences ... is usually right" | `SKILL.md:3` | low |

##### connect-external-service (SKILL.md 78 lines; references 293 + 184 + 92 lines)

Does well: the best progressive-disclosure shape in the batch. SKILL.md is a routing table whose last column names exactly which reference (and which section) to read per row; references are one level deep; rules that apply to every row stay in SKILL.md (the agentskills.io "gotchas stay in SKILL.md" advice); script is run, not read; a `compatibility` field is used correctly.

| # | Offense | Rule | Evidence | Severity |
|---|---|---|---|---|
| 1 | `references/latchkey.md` (293 lines) and `references/mcp.md` (184 lines) have no table of contents. The routing table points at named sections ("Custom services", "Signing in instead of a key") that the agent must search for; latchkey.md has 15 headings across 3 levels. | [A] "For reference files longer than 100 lines, include a table of contents at the top" | `references/latchkey.md`, `references/mcp.md` top of file | medium |
| 2 | Description is second person ("whether or not **you** think a connection already exists"). | [A] third person | `SKILL.md:3` | low |
| 3 | The secret-card procedure is stated three times: SKILL.md:46-66, `direct-api.md:16-46`, `mcp.md:146-157`. The `request_secret.py` invocation block is copied verbatim into all three. | [A] concise; single source | lines cited | medium |
| 4 | The routing table rows are very long single cells (row 2 is about 330 chars, row 4 about 360 chars in one table cell), which makes the table hard to scan and hard to diff; the "Workable when" tests would read better as a per-row bullet. | [A] workflows have clear steps / scannable structure | `SKILL.md:18-25` | low |
| 5 | Time-bound content: `latchkey.md:287-293` "Currently supported services" lists about 27 service names that drift with every latchkey release, immediately followed by "The list that counts is `latchkey services list`", which makes the hard-coded list dead weight. | [A] "Avoid time-sensitive information" | `references/latchkey.md:287-293` | medium |
| 6 | `latchkey.md` opens with two paragraphs (lines 3-6) explaining what latchkey is in general terms the agent already has from SKILL.md and AGENTS.md:113. | [A] concise | `references/latchkey.md:1-6` | low |
| 7 | Terminology drift: the credential UI is called "secret card" (SKILL.md), "card" (direct-api), "approval window" (latchkey.md:153), "sign-in window" (latchkey.md:216); the proxy is "gateway", "latchkey gateway", "the outer host" (latchkey.md:96). | [A] "Pick one term and use it throughout" | spread across the three refs | low |
| 8 | Row 3 (`mcp.md`) and row 5 (`direct-api.md`) both end with a near-identical "What to tell the user" section; SKILL.md's "Never discuss the mechanism unless asked" bullet already covers it. | [A] concise | `direct-api.md:87-92`, `mcp.md:180-184`, `SKILL.md:76-78` | low |
| 9 | `references/latchkey.md` `## Proxying requests through the user's desktop` and `## When the gateway is unreachable` are operational troubleshooting for all rows but live inside the row-1/2/4 reference; an agent on row 3 or 5 never reads them. | [AS] "gotchas stay in SKILL.md, not a separate file the agent may never open" | `references/latchkey.md:252-274` | low |
| 10 | The `agentic-browser-fleet` skill is row 6's "Read" target, so this skill effectively has a fourth reference that is a whole other skill (303 lines) whose description tells the agent to come *here* first; the two skills point at each other. | [AS] coherent units; [A] one-level references | `SKILL.md:25`, `agentic-browser-fleet/SKILL.md:3` | low |

##### notify-user (SKILL.md 62 lines, python entry point)

Does well: tight, one command, exit code is explained and acted on, explicit "Do NOT" list, `compatibility` field used, script is run not read.

| # | Offense | Rule | Evidence | Severity |
|---|---|---|---|---|
| 1 | The when-clause is implicit: the description describes the *message* ("Tell the user ... that a turn in THIS CHAT ended with ...") and never says "use when a turn ends with ..."; the negatives ("Not for chitchat ...") are triggers written as prohibitions. Caps (`THIS CHAT`) are emphasis, not keywords. | [A] "what it does and when to use it" | `notify-user/SKILL.md:3` | medium |
| 2 | Body restates the description nearly verbatim: lines 17-28 are the description's three sentences expanded, then lines 30-37 restate the "do not" list that lines 26-28 just gave. For a 62-line file, about 20 lines are repetition. | [A] concise | `SKILL.md:3` vs `15-37` | low |
| 3 | Entry point path and `compatibility` disagree: `compatibility` says "runs from the root venv (`uv run --no-sync`)" but the command (and the script's own docstring) run it as `python3 ...` with no venv, stdlib only. | [A] consistent terminology; [AS] compatibility describes real requirements | `SKILL.md:4` vs `SKILL.md:48,54` and `notify_user.py` docstring | medium |
| 4 | AGENTS.md:120-123 already carries the command and the exit-code rule; the skill adds the when/when-not judgement only. Acceptable split, but the command is now in two places that can drift. | [A] single source | AGENTS.md:120 vs SKILL.md:48 | low |

##### do-something-new (SKILL.md 62 lines)

Does well: states that it is a thin router and routes to three named flows; explicit "when NOT to use".

| # | Offense | Rule | Evidence | Severity |
|---|---|---|---|---|
| 1 | Description contains behavioural instructions, not triggers: "When doing this, give a very short confirmation message to the user's request, then load this immediately before responding further. Your confirmation message shouldn't mention loading the skill." That is body content injected into the always-loaded catalog (552 chars, 4th longest in the collection). | [A] description "describes what the Skill does and when to use it"; [AS] catalog entries about 50-100 tokens | `do-something-new/SKILL.md:3` | high |
| 2 | Second person throughout the description ("a task **you** haven't done before", "**Your** confirmation message"). | [A] third person | `SKILL.md:3` | low |
| 3 | Unconditional eager load of a 143-line shared file: "`.agents/shared/references/interactive-delivery.md` -- **read it**" (line 18) before the route is even chosen, yet the three routed skills (`fetch-process-show:16`, `build-app:24`) each also say "Read interactive-delivery.md first". The router therefore costs SKILL.md + 143 lines + the target SKILL.md + (the same 143 lines again, if the agent obeys). Only the fourth route ("Neither cleanly applies") actually needs the file here. | [A] progressive disclosure: load "only as needed"; [AS] resources "loaded only when the instructions reference them" | `SKILL.md:15-19, 49-54`; `fetch-process-show/SKILL.md:16`; `build-app/SKILL.md:24` | medium |
| 4 | Step 0 asks the agent to re-scan "the descriptions of the existing skills in `.agents/skills/`", which the harness already injected into context (AGENTS.md:177 "Skill descriptions are auto-injected"). The step duplicates the harness's own matching. | [A] "Can I assume Claude has this information?" | `SKILL.md:21-30` | low |
| 5 | The bail-out script `"There's already a <name> skill for this -- using it instead."` names the skill to the user, contradicting AGENTS.md:179 "Follow a skill without naming it". | internal consistency (AGENTS.md) | `SKILL.md:26` vs AGENTS.md:179 | medium |

##### use-from-phone (SKILL.md 60 lines)

Does well: numbered user-facing steps, one check command, a short "when it does not work" list, platform-specific sub-steps.

| # | Offense | Rule | Evidence | Severity |
|---|---|---|---|---|
| 1 | Time-sensitive UI text: "On recent versions of iOS it is inside the '...' menu next to the address bar", "Enable sharing and web access", "Email me a Mobile Access Link", "Add to Home screen (on some phones it reads 'Install app')" are exact menu labels for Safari, Chrome and Imbue Studio that change with releases, with no "as of" or fallback phrasing. | [A] "Avoid time-sensitive information" | `SKILL.md:20-21, 28, 43, 51` | medium |
| 2 | Description is 370 chars and spends its second sentence summarising the body ("Walks them through turning on sharing, getting the link onto the phone, and adding it to the home screen on iPhone/iPad and Android"). | [AS] catalog cost; [A] description = what + when | `SKILL.md:3` | low |
| 3 | Terminology: "the Share button", "the Share panel", "that same panel", "the panel" used for the same surface; "sharing URL", "the link", "Mobile Access Link", "shared address" for the same URL. | [A] one term throughout | `SKILL.md:10-32` | low |

##### devices (SKILL.md 54 lines)

Does well: short, one endpoint, one header, response shapes shown.

| # | Offense | Rule | Evidence | Severity |
|---|---|---|---|---|
| 1 | Description has no when-clause and no keywords: "List the user's devices (desktops) and route Studio requests to specific ones." A user never says "devices"; they say "my other laptop", "my work computer". The only inbound reference is `file-sharing/SKILL.md:20`. | [A]/[AS] description must say when | `devices/SKILL.md:3` | medium |
| 2 | Body opens with a `## Instructions` section whose only content is a restated when-clause ("Use this skill when you need to know which devices ...") that belongs in the frontmatter description. | [A] description carries triggers; body carries procedure | `SKILL.md:10-13` | low |
| 3 | Name is a bare noun (`devices`), the only one-word generic noun in the collection besides `datalib`; the Anthropic page warns against "Overly generic: documents, data, files". | [A] naming: avoid overly generic names | directory name | medium |
| 4 | It is an API reference (an endpoint, a header, two response shapes) rather than a process: no step says what to do with the list (how to pick, how to ask the user, what to do on 503). The content is a natural section of `connect-external-service/references/latchkey.md`, which already documents `X-Latchkey-Device: *` at lines 68 and 93, so the header is now explained in two skills. | [AS] "Favor procedures over declarations"; single source | `devices/SKILL.md:29-54` vs `latchkey.md:68, 93-96` | medium |
| 5 | Two blank lines between sections, no intro sentence under the H1; minor formatting inconsistency with the rest. | collection consistency | `SKILL.md:14-15, 27-28` | low |

##### handoff-summary (SKILL.md 53 lines)

Does well: states the exact invocation shape, numbered section list for the output, explicit rules, and a "if you cannot write the file" fallback. Correct use of a skill as a machine-invoked slash command.

| # | Offense | Rule | Evidence | Severity |
|---|---|---|---|---|
| 1 | Description says "never by the user", but the catalog entry is still loaded into every chat; nothing (`disable-model-invocation` or equivalent) hides it. Low because the repo's harnesses may not support the field, but the spec's client guide names it. | [AS] client guide: hide `disable-model-invocation` skills from the catalog | `SKILL.md:3` | low |
| 2 | Redundant emphasis: lines 35-43 say "every detail", "Be sure to include any important details", "Do not drop anything", "Err on the side of including a lot of extra detail" in four consecutive paragraphs. | [A] concise; one clear instruction beats four restatements | `SKILL.md:35-43` | low |
| 3 | The 64 KB threshold (line 12) is an implementation constant of the chat app restated in prose; if the app changes it the skill is wrong. | [A] avoid time-sensitive info / voodoo constants | `SKILL.md:12` | low |

##### dealing-with-the-unexpected (SKILL.md 40 lines)

Does well: short; the supervisord log path and `reread && update` sequence are real, useful facts.

| # | Offense | Rule | Evidence | Severity |
|---|---|---|---|---|
| 1 | Dead content: `data/.state/wait_counter` and "the wait script" appear nowhere else in the repository (a grep over the whole checkout matches only this file). Two of the skill's seven concrete checks target a mechanism that no longer exists. | [A] no time-sensitive / stale information | `SKILL.md:19, 26` | high |
| 2 | Teaches nothing the agent lacks: "Read any error messages carefully", "Never panic", "tell the user what you observed / tried / think" are generic. The one domain-specific section (supervisord) is already covered by `check-app-errors` (123 lines, dedicated to exactly this) and AGENTS.md:204. | [A] "Default assumption: Claude is already very smart"; [AS] "Would the agent get this wrong without this instruction?" | `SKILL.md:12-16, 28-40` | medium |
| 3 | Description is second person ("behavior that contradicts **your** docs and prompts") and so broad ("Use when you encounter errors") that it overlaps `assist` (user-reported problems) and `check-app-errors` (service logs) with no boundary stated. | [A] third person; [AC] descriptions must be distinct from one another | `SKILL.md:3` vs `assist/SKILL.md:3`, `check-app-errors/SKILL.md:3` | medium |
| 4 | Name is a gerund phrase (`dealing-with-the-unexpected`), the only gerund in the collection, and non-descriptive of a procedure ("unexpected" is not a keyword a task contains). | [A] consistent naming pattern; avoid vague names | directory name | low |
| 5 | `tmux list-windows -t $(tmux display-message -p '#S')` and "the `bootstrap` tmux window" assume the agent runs inside a tmux session with a window of that name; nothing in the skill says what to do if it does not. | [A] handle error conditions | `SKILL.md:17, 25` | low |

---

##### Cross-cutting: naming across all 41 skills

Directory listing: agentic-browser-fleet, assist, blueprint, blueprint-generate, build-app, caretaker, check-app-errors, connect-external-service, create-theme, crystallize-creation, data-pipeline-builder, datalib, dealing-with-the-unexpected, devices, do-something-new, fetch-process-show, file-sharing, find-transcripts, github-sync, handoff-summary, heal-creation, imbue-studio, launch-custom-agent, launch-task, make-theme-icon, manage-desktop, manage-routines, migrate-workspace, notify-user, publish-template, show-files-in-chat, submit-upstream-changes, update-app, update-creation, update-installed-template, update-published-template, update-self, use-ai-integration, use-from-phone, use-screen, use-template.

Patterns present (Anthropic's page accepts any one pattern but asks for consistency within a collection):

- **Imperative verb-object (26)**: build-app, check-app-errors, connect-external-service, create-theme, crystallize-creation, find-transcripts, heal-creation, launch-custom-agent, launch-task, make-theme-icon, manage-desktop, manage-routines, migrate-workspace, notify-user, publish-template, show-files-in-chat, submit-upstream-changes, update-app, update-creation, update-installed-template, update-published-template, update-self, use-ai-integration, use-from-phone, use-screen, use-template. This is the de-facto house style.
- **Bare nouns / noun phrases (11)**: agentic-browser-fleet, assist (verb, but no object), blueprint, blueprint-generate, caretaker, data-pipeline-builder, datalib, devices, file-sharing, github-sync, handoff-summary, imbue-studio.
- **Gerund (1)**: dealing-with-the-unexpected.
- **Verb-adverb / verb chains (2)**: do-something-new, fetch-process-show (three bare verbs).

Specific collisions and vagueness:

- Five `update-*` names denote four different objects: `update-app` (edit a running app), `update-creation` (verify/extend a skill or app at turn end), `update-self` (pull the upstream template), `update-installed-template` (pull an adopted template), `update-published-template` (push a new template version). The verb carries no information; the agent must read each description to tell them apart, and `update-creation` vs `update-app` overlap on apps by their own descriptions.
- `blueprint` / `blueprint-generate`: one flow, two skills, with the second named noun-verb.
- `devices`, `datalib`, `caretaker`, `assist`, `blueprint`: names that do not say what the procedure does; `devices` is the kind of generic noun the Anthropic page lists as an anti-pattern.
- `imbue-studio` and `agentic-browser-fleet` are named after the product/tool, not the task (the latter's description even says to load `connect-external-service` first instead).
- `file-sharing` and `github-sync` are nouns where siblings are verbs (`share-files`, `sync-github` would match the house style).
- Reserved-word check: no name contains `anthropic` or `claude`.

##### Cross-cutting: descriptions in this batch

| skill | chars | voice | when-clause | instruction leakage |
|---|---|---|---|---|
| do-something-new | 552 | 2nd | yes | yes (how to word the confirmation) |
| connect-external-service | 467 | 2nd | yes | no |
| notify-user | 436 | 3rd | implicit | partly (the "Not for" list) |
| launch-custom-agent | 413 | 3rd | yes | no (feature list padding) |
| use-from-phone | 370 | 3rd | yes | no (body summary padding) |
| show-files-in-chat | 350 | 2nd | yes | no |
| handoff-summary | 193 | 3rd | yes | no |
| dealing-with-the-unexpected | 170 | 2nd | yes (too broad) | no |
| blueprint-generate | 99 | 3rd | weak | no |
| blueprint | 86 | 3rd | none | no |
| devices | 78 | 3rd | none | no |

Note: all 11 carry `metadata.author: imbue`, a field no spec consumer reads; harmless but adds a line to every file. None use `allowed-tools`. `compatibility` is used by `connect-external-service` and `notify-user`; the latter's is inaccurate (see notify-user #3).

##### Out of scope, noticed in passing

- `agentic-browser-fleet/SKILL.md:3` tells the agent to load a *different* skill first; that belongs in the other batch's catalog.
- `fetch-process-show:16` and `build-app:24` both say "Read interactive-delivery.md first" unconditionally; together with `do-something-new:18` that is three skills eagerly loading the same 143-line file.

### Appendix G: the shared layer under .agents/shared/

#### Audit: the `.agents/shared/` layer (references + worker docs)

Scope: 14 files under `.agents/shared/references/` and 13 under `.agents/shared/worker/` (SKILL.md + 12 references), 3,973 lines / ~34k words in total. Python scripts skipped. All paths below are relative to the dwt repo root. Line numbers are from the current checkout (branch `dwt-audit-agent-skills-best-practices`, HEAD 949248b6a).

Rules cited: **[A]** Anthropic best-practices page, **[C]** Academy course, **[S]** agentskills.io spec / best-practices, **[R]** the repo's own `spec-summary.md` / `type-skill.md` ("Keep SKILL.md under ~500 lines; split long content into references/"; "load on demand").

##### 0. Facts about the layer

| File | Lines | Words | ~Tokens | Direct SKILL.md inbound | Other inbound |
|---|---|---|---|---|---|
| references/lead-proxy.md | 409 | 3285 | 4.4k | 9 skills | 2 skill refs, 3 shared |
| references/harden-contention.md | 405 | 3542 | 4.8k | 5 skills | 2 skill refs, 2 shared |
| references/spec-summary.md | 277 | 2024 | 2.7k | 0 | 1 skill ref, 4 shared, app_manifest code |
| references/worker-reporting.md | 236 | 1532 | 2.1k | 7 skills | 2 skill refs, 6 shared |
| references/running-python.md | 201 | 1696 | 2.3k | 0 | 1 skill ref, 4 shared, AGENTS.md |
| references/service-processes.md | 197 | 1428 | 1.9k | 2 skills | 1 skill ref, 1 shared |
| references/interactive-delivery.md | 143 | 1369 | 1.8k | 4 skills | 1 skill ref |
| references/user-facing-language.md | 117 | 1052 | 1.4k | 1 skill | AGENTS.md, visiting-agents.md |
| references/visiting-agents.md | 66 | 770 | 1.0k | 1 skill (build-app) | share_gateway code (string template) |
| references/freeing-memory.md | 56 | 523 | 0.7k | 1 skill | 2 skill refs, lead-proxy, AGENTS.md |
| references/element-references.md | 45 | 558 | 0.8k | 1 skill | AGENTS.md |
| references/report-built-in-issues.md | 44 | 443 | 0.6k | 2 skills | 1 skill ref, lead-proxy, AGENTS.md |
| references/visiting-agents-invite.md | 27 | 368 | 0.5k | 0 | share_gateway code (string template) |
| references/continue-chat.md | 16 | 140 | 0.2k | 0 | chat app code (string template) |
| worker/SKILL.md | 112 | 1007 | 1.4k | 7 skills | 12 skill refs, 10 shared |
| worker/references/harden-creation.md | 552 | 5228 | 7.1k | 2 skills | 3 skill refs, 12 shared, AGENTS.md |
| worker/references/op-update.md | 201 | 1464 | 2.0k | 1 | 1 skill ref, type-app.md, worker table (`op-<OPERATION>`) |
| worker/references/op-crystallize.md | 151 | 1239 | 1.7k | 1 (build-app) | worker table |
| worker/references/verification.md | 133 | 1019 | 1.4k | **0** | **0 (orphan)** |
| worker/references/op-heal.md | 125 | 890 | 1.2k | 0 | worker table |
| worker/references/type-skill.md | 118 | 870 | 1.2k | 0 | 1 skill ref, worker table |
| worker/references/type-app.md | 103 | 839 | 1.1k | 1 (build-app) | 2 skill refs, 5 shared |
| worker/references/type-service.md | 72 | 575 | 0.8k | 0 | 1 skill ref, worker table |
| worker/references/web-frontend-testing.md | 54 | 494 | 0.7k | 0 | 1 skill ref, 3 shared |
| worker/references/skill-outline-fields.md | 43 | 401 | 0.5k | 0 | 3 shared |
| worker/references/update-vs-create-new.md | 38 | 304 | 0.4k | 0 | 3 shared |
| worker/references/transcript-exploration.md | 26 | 192 | 0.3k | 0 | 4 shared |

Every file path a shared file names exists on disk (checked all `.py`/`.sh`/`.md` paths; none missing).

##### 1. Depth: the link graph (rule: "keep references one level deep from SKILL.md" [A], "avoid deeply nested reference chains" [S])

**1.1 (high) The shared layer is a second and third tier, not a first.** Every skill that delegates to it links one shared file, and that file links onward. Concrete chains, each hop verified by grep:

- `update-self/SKILL.md` -> `harden-contention.md` (direct) -> `lead-proxy.md` (harden-contention.md:181, :207, :245) -> `launch-task/references/worker-failure.md` (lead-proxy.md:101, :316) -> `launch-task/references/dead-worker-recovery.md` (worker-failure.md) -> `freeing-memory.md` (dead-worker-recovery.md) -> `update-self/references/freeing-memory-for-an-update.md` (freeing-memory.md:55). **Six hops**, ending back inside the skill it started from.
- `heal-creation/SKILL.md:120` -> `worker/SKILL.md` -> `op-heal.md` (worker/SKILL.md:60, by template `op-<OPERATION>.md`) -> `type-app.md` (op-heal.md:18) -> `op-update.md` "Critical-app handoff" (type-app.md:83) -> `web-frontend-testing.md` (op-update.md:53) -> `harden-creation.md` (web-frontend-testing.md:7) -> `harden-contention.md` (harden-creation.md:89) -> `lead-proxy.md` (harden-contention.md:181). **Eight hops.**
- `crystallize-creation/SKILL.md:139` -> `worker/SKILL.md` -> `type-skill.md` (table row) -> `spec-summary.md` (type-skill.md:3) -> `running-python.md` (spec-summary.md, "See ... running-python.md for why") and -> `use-ai-integration/SKILL.md` (spec-summary.md, "make sure to read and follow") -> `launch-task` (use-ai-integration links it). **Four to five hops.**

**1.2 (high) The graph is cyclic.** `harden-creation.md` -> `harden-contention.md` (:89) -> `lead-proxy.md` (:181) -> `worker-reporting.md` (:363) -> `harden-creation.md` (:75, :201). Also `type-app.md` <-> `op-update.md` <-> `web-frontend-testing.md` <-> `type-app.md`. [A] warns that past one level Claude "might partially read files"; a cycle gives no stopping point at all.

**1.3 (medium) Cross-file pointers address bold run-in labels, not headings.** `op-heal.md:54` points to `harden-creation.md` "A regression the gate let through"; `type-app.md:83` points to `op-update.md` "Critical-app handoff"; `harden-contention.md:338` to `dead-worker-recovery.md` "Last resort: manual worktree salvage". In each case the target is a `**bold.**` paragraph lead (harden-creation.md:403, op-update.md:33), so a heading skim or a `grep '^#'` does not locate it. Of 52 quoted section pointers checked, the heading-level ones all resolve; the run-in-label ones are the ones that do not.

**1.4 (medium) Second-level files are reached by a *template*, not a link.** `worker/SKILL.md:60-61` names `op-<OPERATION>.md` and `type-<TYPE>.md`. `op-heal.md`, `type-service.md`, `type-skill.md` therefore have zero literal inbound links from any SKILL.md and show up as orphans to any link checker ([A]: "explicit markdown links that say when to read the file").

**1.5 (low) Skill-local references link into the shared layer and back.** 12 skill reference files (10 of them under `update-self/references/`) link `worker/SKILL.md`; `launch-task/references/worker-failure.md` links both `lead-proxy.md` and `harden-contention.md`. So the "references one level deep" rule is broken from both ends: skill -> skill-ref -> shared -> shared.

##### 2. Size and navigability

**2.1 (high) Two shared files exceed the 500-line budget meant for a whole SKILL.md, and 14 more exceed 100 lines; none has a table of contents** ([A]: "For reference files longer than 100 lines, include a table of contents"). The 16 files over 100 lines: `harden-creation.md` (552 lines, 17 H2), `lead-proxy.md` (409, 9 H2), `harden-contention.md` (405, 5 H2 for 3.5k words), `spec-summary.md` (277), `worker-reporting.md` (236), `running-python.md` (201), `op-update.md` (201), `service-processes.md` (197), `op-crystallize.md` (151), `interactive-delivery.md` (143), `verification.md` (133), `op-heal.md` (125), `type-skill.md` (118), `user-facing-language.md` (117), `worker/SKILL.md` (112), `type-app.md` (103).

**2.2 (high) Effective load of one worker run.** `worker/SKILL.md:52` says "Read these top to bottom, then follow them." Summing only the rows the table marks unconditional:

| Run | Files read unconditionally | Lines | ~Tokens |
|---|---|---|---|
| crystallize a skill | worker/SKILL.md, harden-creation, op-crystallize, type-skill, worker-reporting, transcript-exploration, spec-summary, skill-outline-fields | 1,515 | ~16.9k |
| committed update of an app | worker/SKILL.md, harden-creation, op-update, type-app, web-frontend-testing, worker-reporting | 1,258 | ~14.3k |

Plus what those name as required reading before the work starts: `spec-summary.md` sends an `[ai-script]` skill to `use-ai-integration/SKILL.md` (235 lines) and to `running-python.md` (201); `harden-creation.md:350` sends the test gate to `system/libs/app_manifest/README.md` (413); a split pass adds `launch-task/SKILL.md` (212) and `lead-proxy.md` (409). A skill crystallize that touches all of these reads ~2,900 lines (~35k tokens) of instructions before writing a line of code. [S] recommends the activated instruction tier stay under 5,000 tokens.

**2.3 (high) Effective load of the lead side is the same order.** `heal-creation/SKILL.md` (203 lines) requires the single-flight check in `harden-contention.md` before dispatch (:60), `worker-reporting.md` for the task-file fields (:82), and `lead-proxy.md` to drive the worker (:156): 1,253 lines, ~13.1k tokens, for a flow whose SKILL.md is itself comfortably small. The same holds for `update-creation` (:93, :133, :221) and `crystallize-creation` (:86, :208).

**2.4 (medium) Always-read files carry debugging-only and rarely-needed material.** `lead-proxy.md:380-409` "`mngr rsync` rationale" opens with "The launcher makes this transfer itself... Read this when you are debugging one" yet sits in the file every lead reads. `harden-contention.md:247-406` (160 lines: taking over an orphaned pass, superseding a gone worker, the two reverted-milestone wrinkles) is the rare path, in the same file as the three pre-merge checks every merge needs. `lead-proxy.md:354-378` "When you are a worker yourself" applies only to intermediate leads. Remedy: split the rare paths into their own files and leave one-line conditional pointers ([A] "conditional details" pattern).

**2.5 (medium) Generic engineering advice the model already has.** `harden-creation.md:190-235` ("Optimize independent work", "Tolerate partial failure and support resumption") and :236-245 restate concurrency, bounded fan-out, deterministic ordering, per-item failure isolation and incremental persistence at length ([A] "Does Claude really need this explanation?"; [S] "omit what it knows"). `running-python.md:16-20, :148-152, :193-195` explain how `uv run` syncs and what it costs in ms/MB; the rule is one line (:6-8).

##### 3. Duplication

**3.1 (medium) The three op-* files share their scaffolding verbatim.** "Load this alongside `harden-creation.md` (the universal contract) and your creation reference..." appears in op-crystallize.md:4-6, op-update.md:4-6, op-heal.md:4-6. "Ensure all review gates have been run per `harden-creation.md`, before the final gate report, so the user sees a single report..." appears in op-crystallize.md:107-108, op-update.md:144-145, op-heal.md:74-75. "In a pass over several creations, write one block per creation..." (op-update.md:183-187, op-heal.md:111-115). The four-line report-tail block (`References registered / Outside footprint / Undeclared couplings / Flaky tests`) appears **nine times** across op-crystallize.md (:122-125, :138-141), op-update.md (:63-64, :161-164, :174-177), op-heal.md (:29-30, :89-92, :102-105). A change to one field name is a nine-site edit.

**3.2 (medium) Lead-side and worker-side describe the same mechanics twice.** Milestone delivery: `worker-reporting.md:107-177` and `lead-proxy.md:181-233` each explain the `consumed/` archive, the short-sha naming, and re-delivery idempotence. Catch-up: `harden-contention.md:195-245` and `harden-creation.md:455-527` each narrate the merge/resolve/re-gate cycle, with the lockfile carve-out stated in both (harden-contention.md:165-175, harden-creation.md:469-471). `uv sync` after a merge that touched `pyproject.toml`/`uv.lock` is spelled out twice inside lead-proxy.md alone (:207-213 and :290-296) and again in running-python.md:32-34 and type-app.md:46-48.

**3.3 (low) `spec-summary.md` restates the agentskills.io spec, then presents repo policy as spec.** Lines 1-5 and 88-130 restate directory layout and frontmatter constraints that `validate_skill.py` already enforces and `type-skill.md`/`skill-outline-fields.md` repeat (name rules appear in spec-summary.md:117-120, in type-skill.md via "naming rules in spec-summary", and in validate_skill's docstring). The remaining ~230 lines (`[script]`/`[ai-script]`/`[prose]` step kinds, `run.py` subcommand shape, packaging, scenario template) are repo conventions that the spec does not contain, under a title that says "agentskills.io layout cheat sheet" and an opener that says "The crystallized skills in this project follow the agentskills.io spec". Remedy: rename to the repo's skill conventions and drop the spec restatement.

**3.4 (low) The "Reliability is the floor; simplicity is the target" line** that the design spec (`docs/system/specs/skill-lifecycle-shared-refs/concise.md`, "Preserve-as-is") said would stay in five SKILL.md homes now exists in exactly one place, `type-skill.md:15`. Not duplication today, but the spec document describing the layer is stale (see 5.3).

##### 4. Content

**4.1 (medium) Time-bound and migration text in always-read files** ([A]: "avoid time-sensitive information"):
- `worker-reporting.md:20-22` "older launchers stamped its name..."; :31-36 "a launcher that predates launch-time stamping does not write them"; :159-161 "spelled out because a milestone has no subcommand yet".
- `harden-contention.md:30-32` "which is how a ticket opened before titles carried types names its creation".
- `service-processes.md:73-75` "This is the replacement for the old `restart = "on-failure"`"; :61 and :187 "legacy names like `system_interface`".
- `running-python.md:17-20, :148-149, :193-195` measured numbers ("roughly 100-140 ms", "about 40 ms", "about 19 MB each", "450-560 ms") used to justify rules; these are voodoo constants in prose form and will rot silently.
- `harden-creation.md:423` a hardcoded example timestamp in a command the worker is told to adapt (fine as an example, but the `STARTED_AT` dance exists only because "a shell variable does not survive from one command to the next", a harness quirk explained inline).

**4.2 (medium) Inconsistent terminology for one concept** ([A]: "pick one term"). Counts across the 27 files: the child agent is a "worker" (259), "sub-worker" (21), "sibling" (21), "sub-agent" (3), "background agent" (3, the user-facing word per AGENTS.md). The per-type file is "your creation reference" (9), "creation reference" (11), "type reference" (1), and `type-<TYPE>.md`. The parent is "the lead" (95), "calling skill" (7), "calling flow" (2). The milestone copy is a "push" (worker-reporting.md:174-175 "Every later push re-delivers the file") although the mechanism is `cp` (:163-167). The design spec calls the section "mngr push rationale"; the file now says "mngr rsync rationale" (lead-proxy.md:380).

**4.3 (medium) Prompt templates filed as references.** `continue-chat.md` (`${title}`, `${summary}`...), `visiting-agents.md` and `visiting-agents-invite.md` (`{{workspace_name}}`, `<!-- exec-only:start -->` markers) are string templates rendered by `system/apps/chat/imbue/chat/chat_handoffs.py` and `system/services/share_gateway/.../agent_orientation.py`. They are not instructions for an agent reading `references/`, have no inbound link from any skill except `build-app` naming `visiting-agents.md`, and the exec-only markers are meaningless to a reader. [A]: "Organize for discovery"; a `references/` directory should hold reference material.

**4.4 (medium) `verification.md` is an orphan that is still being edited.** Zero inbound links anywhere (skills, shared files, AGENTS.md, code). Commit 8c547bdbe (2026-09-10, "Keep the harden contract from routing workers to the parked review gates") deliberately removed the only pointer because the gates it describes are "parked", yet the file received five further commits on 2026-10-13 (e857eb294, 0c2c2afaf, 8d3128a11, 803a1e635, f26e2defc) and `harden-creation.md:320-324` "Review gates" now consists of two steps that never mention it. Meanwhile `lead-proxy.md:87` still tells leads that "autofix, verify-architecture" stages "can legitimately exceed 30 minutes", and the changelog `worker-plugin-install.md` says workers get the plugin so that "/autofix and /verify-architecture exist" for their "mandated review gates". Either the gates run (then the file must be linked) or they do not (then it and lead-proxy.md:87 are dead text). Also a vague file name: "verification" of what.

**4.5 (low) `worker/SKILL.md` violates the spec it is held to by shape.** Frontmatter `name: harden-worker` in a directory named `worker` ([S]: name "must match the parent directory name"; the repo's `validate_skill.py` rejects exactly this). It is kept out of `skills/` so the loader ignores it, but the design spec says `.agents/shared/` "contains no SKILL.md so the skill loader ignores it" (concise.md, "Overview" and "Expected Behavior") and `AGENTS.md:153` says skills are discovered under `.agents/skills/`; a harness that scans for any `SKILL.md` (agentskills.io clients scan "max depth 4-6") would surface `harden-worker` as an invocable skill.

**4.6 (low) Options without a default.** `lead-proxy.md:25-28` names both `await` and `launch-sync`; `service-processes.md:166-171` offers `supervisorctl reread && update`, `verify_app.py --restart`, or `supervisorctl restart` by case; `running-python.md:24-36` lists four sync situations. Each is correct, but [S]/[A] prefer "one default plus an escape hatch".

**4.7 (low) Emphasis and absolutes.** `web-frontend-testing.md:44-45` "you MUST look at the actual rendered page"; `verification.md:11-12` "You are not permitted to skip gates"; `harden-creation.md:446`, `lead-proxy.md:55` bold NEVER/never rules. [S] "Explain the why... reasoning-based instructions work better than rigid directives". Most of these do carry a why, so this is minor.

**4.8 (low) Repo-root-relative paths assume cwd.** Every pointer is `.agents/shared/...` rather than a path relative to the file ([S]: "relative paths from the skill root"). The three lead skills use proper relative markdown links for harden-contention (`../../shared/references/harden-contention.md`, heal-creation.md:60) but bare `.agents/...` strings everywhere else; inside the shared layer only `service-processes.md:44, :66` uses a relative markdown link. A worker in a worktree (cwd `/home/user/worktrees/...`) resolves them only because every worktree is a full checkout.

##### 5. Is a shared layer consistent with the spec's model?

**5.1 (medium) It breaks the "skill is a self-contained directory" model.** [S] and [A] define resources as files under the skill's own directory, loaded by paths relative to it. 19 of 41 skills (and 30 skill reference files) depend on files outside their directory. Consequences:
- `publish-template/scripts/build_template.sh` stages only explicit `--include` paths (lines 82-130, 237-315) over a clean base checkout (:357-362); `write_template_manifest.py` lints only secrets declarations (:23, :289). Nothing checks that a published skill's `.agents/shared/...` pointers exist in the adopter's base, or at a compatible version. `publish-template/references/assembly-worker.md:177-178` already lives with one instance of this skew ("step 1 replaced `.agents/skills/launch-task/` with the base's copy, which may predate the launcher's `report` subcommand").
- `use-template/SKILL.md` has no handling for an adopted skill whose shared references differ from or are missing in this workspace (no mention of `.agents/shared` in it).
- `worker/SKILL.md:42-44` has to invent a fourth creation type, `shared`, "which you harden as a skill (`type-skill.md`)", because the layer has no manifest, no footprint and no tests of its own in the app-manifest model.
- Third-party skills (`.agents/third-party-skills.toml`) are explicitly kept self-contained; the built-ins are not, so the two populations follow different rules.

**5.2 (low) It is, however, the intended trade.** `docs/system/specs/skill-lifecycle-shared-refs/concise.md` records the goal: three lifecycle skills were duplicating "substantial prose" and the layer dedups it with "one-level references into `.agents/shared/references/`". The one-level promise is the part that did not hold (section 1).

**5.3 (low) The design spec is stale against the tree.** It names skills `crystallize-task` / `heal-skill` / `update-skill` and worker sub-skills under `assets/worker/`, an `install_worker_skills.sh`, `extract_turn.py` and `transcript_parsing.py` in `.agents/shared/scripts/`, a `FLOW: absorb|verify` marker, and "no SKILL.md" in `.agents/shared/`. The tree has `crystallize-creation` / `heal-creation` / `update-creation`, one generic `worker/SKILL.md`, `ORIGIN: committed|emergent` (op-update.md:22), none of the named scripts, and `update-vs-create-new.md` under `worker/references/` rather than `references/`. Remedy: mark the spec historical or rewrite its "Expected Behavior" to the current layout.

##### 6. What the layer does well

- `worker/SKILL.md:57-67` is a genuine progressive-disclosure index: a table of "Load when / Reference / What it gives you" with the two conditional rows defined precisely (:69-80). This is the pattern [A] recommends and most of the 41 SKILL.md files lack.
- The operation x type composition (`op-*.md` x `type-*.md`) avoids a 3x3 matrix of near-identical worker documents; the op files are short (125-201 lines) and reference-first.
- Descriptive file names throughout (`harden-contention.md`, `update-vs-create-new.md`, `transcript-exploration.md`), no `misc.md`.
- `user-facing-language.md` and `report-built-in-issues.md` are single sources of truth that AGENTS.md and several skills defer to by name rather than restating.
- `freeing-memory.md`, `transcript-exploration.md`, `update-vs-create-new.md`, `skill-outline-fields.md` are the right size for one concern and are linked with a stated condition.
- Every path named in the layer resolves on disk; the quoted heading pointers that target real headings all resolve.

##### 7. Offense summary by severity

| # | Severity | Offense | Where |
|---|---|---|---|
| 1.1 | high | Reference chains of 4-8 hops from SKILL.md | update-self, heal-creation, crystallize-creation chains above |
| 1.2 | high | Cyclic reference graph | harden-creation <-> harden-contention <-> lead-proxy <-> worker-reporting |
| 2.1 | high | 16 files over 100 lines, none with a TOC; two over 400 lines | harden-creation (552), lead-proxy (409), harden-contention (405) |
| 2.2 | high | One worker run reads 1,258-1,515 lines unconditionally (14-17k tokens), up to ~2,900 with named follow-ons | worker/SKILL.md table + spec-summary/harden-creation pointers |
| 2.3 | high | Lead side reads ~1,250 lines before dispatch | heal-/update-/crystallize-creation + harden-contention + lead-proxy + worker-reporting |
| 1.3 | medium | Section pointers target bold run-in labels, not headings | op-heal.md:54, type-app.md:83, harden-contention.md:338 |
| 1.4 | medium | Second tier reached by filename template, invisible to link checks | worker/SKILL.md:60-61 |
| 2.4 | medium | Rare/debug paths inside always-read files | lead-proxy.md:380-409, :354-378; harden-contention.md:247-406 |
| 2.5 | medium | Generic engineering advice | harden-creation.md:190-245; running-python.md explanations |
| 3.1 | medium | Verbatim scaffolding and a 4-line report tail repeated 9x across op-* files | op-crystallize/op-update/op-heal |
| 3.2 | medium | Lead and worker sides each narrate milestones, catch-up, uv sync | worker-reporting/lead-proxy; harden-contention/harden-creation |
| 4.1 | medium | Time-bound text and measured constants | worker-reporting.md:20-36,:159-161; harden-contention.md:30-32; service-processes.md:73-75; running-python.md |
| 4.2 | medium | worker/sub-worker/sibling/sub-agent; creation reference/type reference; push vs cp | layer-wide |
| 4.3 | medium | Code string templates filed under references/ | continue-chat.md, visiting-agents*.md |
| 4.4 | medium | Orphaned but actively edited verification.md; stale gate mentions | verification.md; lead-proxy.md:87 |
| 5.1 | medium | Skills are not self-contained; publish/use-template do not check shared pointers | build_template.sh, write_template_manifest.py, use-template |
| 1.5 | low | Skill refs link into shared and back | update-self/references/*, launch-task/references/worker-failure.md |
| 3.3 | low | spec-summary.md restates the spec and labels repo policy as spec | spec-summary.md:1-5, 88-130 |
| 3.4 / 5.3 | low | Design spec stale against the tree | docs/system/specs/skill-lifecycle-shared-refs/concise.md |
| 4.5 | low | worker/SKILL.md name/dir mismatch; a SKILL.md where the spec says none | worker/SKILL.md:2 |
| 4.6 | low | Options without a stated default | lead-proxy.md:25-28; service-processes.md:166-171 |
| 4.7 | low | Absolutes without the why | web-frontend-testing.md:44; verification.md:11 |
| 4.8 | low | Repo-root-relative pointers, mixed with a few relative links | layer-wide |

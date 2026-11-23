# Rationale: where each skill authoring rule comes from

The skill authoring guide (`docs/system/skill_authoring_guide.md`, vendored from mngr) states the rules.
`report-skills-best-practices-audit.md` and `plan-skills-best-practices.md` beside this file record what was wrong and what was done.
This file records why each rule is what it is: the published source it rests on, or the decision that set it where no source did.
Read it before changing a rule.

## Contents

- Sources
- How Anthropic's own skills follow the rules
- What the vendors agree on for descriptions
- Published guidance on content two skills need
- Rule by rule
- What four harnesses keep after a compaction
- Considered and not adopted
- Not traced

## Sources

Read in full for the audit (2026-11-04) unless a later date is given.

| Key | Source |
|---|---|
| Anthropic | "Skill authoring best practices", <https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices> |
| Academy | Claude Academy, "Configuration and multi-file skills", <https://academy.claude.com/courses/introduction-to-agent-skills/configuration-and-multi-file-skills> |
| agentskills.io | The Agent Skills specification and its guides, <https://agentskills.io/specification> |
| Claude Code skills | "Extend Claude with skills", <https://code.claude.com/docs/en/skills> |
| Claude Code compaction | "How Claude Code works", "When context fills up", <https://code.claude.com/docs/en/how-claude-code-works#when-context-fills-up> (read 2026-11-18) |
| skill-creator | `anthropics/skills`, `skills/skill-creator` (its SKILL.md, `improve_description.py`, `quick_validate.py`), <https://github.com/anthropics/skills> |
| Engineering post | "Equipping agents for the real world with Agent Skills", <https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills> |
| OpenAI | Codex skills docs, <https://developers.openai.com/codex/skills> and <https://learn.chatgpt.com/docs/build-skills>; the plugin page, <https://developers.openai.com/plugins/build/skills> (read 2026-11-04 and 2026-11-05) |
| superpowers | `obra/superpowers`, the `writing-skills` skill, <https://github.com/obra/superpowers/tree/main/skills/writing-skills> |
| nyosegawa | "Agent Skill Best Practices for Claude and Codex", <https://github.com/nyosegawa/skills/blob/main/agent-skill-best-practices.md> |
| skill-lint | `himself65/skill-lint`, a community linter, <https://github.com/himself65/skill-lint> |
| arXiv 2607.01456 | "From Anatomy to Smells: An Empirical Study of SKILL.md in Agent Skills", <https://arxiv.org/abs/2607.01456> |
| arXiv 2608.08453 | "What Keeps Agent Skills from Being Reusable? Evidence from 138K SKILL.md Files", <https://arxiv.org/abs/2608.08453> |
| Spec discussions | agentskills discussion 374 (sharing knowledge across skills) and issue 100 (skills depending on skills), <https://github.com/agentskills/agentskills/discussions/374>, <https://github.com/agentskills/agentskills/issues/100> (read 2026-11-17) |
| addyosmani | `addyosmani/agent-skills` issue 329, why its shared references are not colocated, <https://github.com/addyosmani/agent-skills/issues/329> (read 2026-11-17) |

Two more were read and gave no rule: Simon Willison's post on skills (framing only) and the `humanlayer/skills` repository (no authoring guidance).
The support article "Creating custom skills" gives a description limit of 200 characters, which conflicts with 1,024 everywhere else and was treated as stale.

## How Anthropic's own skills follow the rules

On 2026-11-04 the same checks the audit ran over this tree were run over `anthropics/skills` (19 skills at `ca45fc4`, 2026-10-28).
The result decided how strictly several rules are held here.

| Published rule | In Anthropic's own repository |
|---|---|
| SKILL.md under 500 lines | 18 of 19 comply. `claude-api` is 608 lines. |
| Description under 1,024 characters, third person, what and when | `claude-api` is 1,068 characters. 3 of 19 use second person. The document skills run 835 to 993 characters on purpose: dense trigger phrases and an explicit "Do NOT use for" boundary. |
| Contents list on references over 100 lines | Never followed: 51 reference files over 100 lines, none with a contents list. |
| References one level deep | Followed by the simple skills (`pdf`, `docx`, `pptx`, `xlsx`, `frontend-design`). Not by `claude-api`, whose 60-odd references link each other, nor by two `mcp-builder` references that link each other. |
| Every file linked from SKILL.md | Loosely. `pdf` names none of its 8 scripts in SKILL.md. |
| No time-sensitive content | `claude-api` is version-bound by nature and uses the sanctioned form: the old pattern named, the replacement given. |

Nothing enforces these there: `quick_validate.py` checks frontmatter only.
What Anthropic holds itself to is the 500-line body, descriptions that say what and when with real trigger vocabulary, scripts that are run and not read, and a flat reference tier for ordinary skills.
The one-level rule bends for a large domain, and the contents-list rule is not practised.

## What the vendors agree on for descriptions

The description rules were checked against OpenAI's guidance on 2026-11-05, so the house shape would not be tuned to one harness.

- **OpenAI.** "Explain exactly when this skill should and should not trigger." "Write concise descriptions with clear scope and boundaries. Front-load the key use case and trigger words so a host can still match the skill if descriptions are shortened." On the plugin page: "State the workflow and the conditions that should trigger it. Put detailed procedure, format, and safety instructions in the body."
- **agentskills.io.** What the skill does, when to use it, and the specific keywords.
- **Anthropic.** The same, in third person.

The shared core is: what it does, when it should and should not trigger, triggers first, concise, no procedure.
Third person is Anthropic's alone; OpenAI and agentskills.io are silent on voice.
"No first or second person" was kept as a cheap rule that costs nothing on the other harnesses.

OpenAI gives no size limit for SKILL.md. Its only numbers are for the catalog.

## Published guidance on content two skills need

Searched on 2026-11-17. No major source covers it: each treats a skill as one self-contained directory.

- **Anthropic** covers references inside one skill only.
- **agentskills.io.** A spec maintainer called shared knowledge out of scope (discussion 374) and offered two answers: point the agent at a shared external source, or sync the content into each skill with a build step. Skill-to-skill dependencies are an open issue (100).
- **Claude Code** has one sentence: `${CLAUDE_PLUGIN_ROOT}` reaches files "shared between the plugin's skills".
- **OpenAI** is silent.

Community practice has three approaches: a shared directory beside the skills (`addyosmani/agent-skills`, for one source of truth, at the cost that a skill installed alone loses its shared files), a copy in each skill (drift), and inlining at build time (generated files).
This tree uses the first. Its known cost is what `update-self`'s staging of the shared layer handles.
The link-direction rule, the one-reader rule and the checks that enforce them are this project's own; nothing like them was found published.

## Rule by rule

### Size

| Rule | Rests on |
|---|---|
| SKILL.md body at most 500 lines | The published limit: Anthropic, Academy, agentskills.io, Claude Code skills, skill-creator. The validator already had it. Behind it, the observation that an agent reads a long file in portions and may stop early, so only a short file is read whole. That observation has no published source. It is why the limit would hold even on a harness that never compacts. |
| SKILL.md at most 14,000 bytes | The observation that a compaction keeps only the start of a skill. Claude Code skills: it keeps "the first 5,000 tokens" of each invoked skill, 25,000 combined, and does not re-read the file. Claude Code compaction: of the files a session merely read or edited it re-reads up to five, most recently modified first, and one over 5,000 tokens comes back as a path only, so moving detail to a reference does not protect it. agentskills.io and skill-lint also say about 5,000 tokens. At the measured 2.82 bytes per token that is 14 KB, chosen over 20 KB and over no byte cap (2026-11-09). |

The byte figure is measured: `count_tokens` over 111 markdown files gave 2.82 bytes per token with a 5% spread, so a 500-line skill here is about 10,000 tokens, twice the budget.
Bytes were chosen as the unit because they were the steadiest proxy: tokens per word spread 11.5% and tokens per line 17%, and words times 1.35 was low by 1.8 times for this prose.
The docs do not say which tokenizer the 5,000 is counted on; on the older one the budget is about 18.7 KB.
The validator's failure text states the goal, that an agent reads and keeps the whole file, and leaves the compaction mechanism to the guide.

A reference has no size limit. That was decided against: the contents-list rule and the review category cover references.

### SKILL.md

| Rule | Rests on |
|---|---|
| The checklist, not the manual; move out what most runs do not need | Anthropic ("like a table of contents"), Academy, agentskills.io, Engineering post; nyosegawa lists a SKILL.md holding the whole manual as an anti-pattern. The in-repo precedent is `publish-template`'s restructure (commit `8e327598e`). |
| Standing instructions and the step list first | Claude Code skills: "put the most important instructions near the top of SKILL.md". |
| Standing instructions are rules, not steps | Claude Code skills: the file is not re-read on later turns, "so write guidance that should apply throughout a task as standing instructions rather than one-time steps". |
| A copyable step list | Anthropic and agentskills.io: numbered steps and a checklist for a multi-step flow. |
| Each step names its reference | At best only the invoked SKILL.md is re-attached after a compaction, so detail moved to a reference is protected only if the step says what to re-read. |
| The after-compaction rule lives in the instructions file | An instruction to re-read that sits in a reference is lost with the reference, so it cannot live there. A line in every SKILL.md spends each skill's 14 KB on the same sentence. Every harness tested still had the instructions file after a compaction, so one statement there reaches every skill. |
| The rule names the reference first, and SKILL.md only when its text is gone | The harness test below: Claude Code re-attaches a compliant SKILL.md whole, so re-reading it there is a wasted call, while Pi and Codex re-attach nothing. A condition the agent can check for itself (is the text still in front of it) holds on each of them, and each agent's own report of what it could see was accurate. |
| The rule sits near the top of the instructions file | The same test: Codex gives the agent the first 32,768 bytes of `AGENTS.md` (`project_doc_max_bytes`) and Antigravity the first 24,000 bytes (its documented limit on one rule file), of a file that is 44 KB. A rule further down reaches neither. |
| One term per concept; one default with an escape hatch | Anthropic, agentskills.io. |

### Step lists and contents lists

| Rule | Rests on |
|---|---|
| A contents list in a reference over 100 lines | Anthropic (skill-creator says 300). Anthropic does not practise it, and the first proposal was to leave it mostly alone; it was kept at 100 because an agent sent to one section of a long file has no other way to find it. |
| A step list in a SKILL.md over 200 lines | No source. Nobody prescribes a contents list for SKILL.md, because in the published model SKILL.md is the contents list. This is the equivalent, set on 2026-11-05. |
| `## Steps` numbered for a procedure, `## Contents` bulleted otherwise | A procedure's list is the checklist the agent works down in order, and a numbered entry lets it say and find where it is. A file that is not a procedure has no order to number. Calling both "Contents" hid which kind of file the agent was in. |
| One entry per section, never two sections in one entry | An agent finds its place by matching an entry to a heading. An entry that covers two sections matches neither, and the validator, which checks entries against the H2 headings, cannot check it. |
| The step list is the whole procedure | An agent that works from the list does only what the list names. `crystallize-creation`'s list had the steps around the work and no step that did it. |
| Below a step list every H2 is a step; a value the steps use is chosen in the step that first uses it | So the list and the sections match one to one and nothing the agent must do sits outside the list. A section above the steps that told the agent to name the pass was work the list did not carry. |
| A heading names an action or a topic; text sent to the user goes in a fenced block | An agent finds a section by its heading, so the heading has to say what the section is for. One heading was the opening line of a message to the user ("Hi, I'm a Caretaker for your workspace"), which says nothing about the step it headed and reads as prose to act on. |

### References

| Rule | Rests on |
|---|---|
| A link is a path; a filename pattern links nothing | The harden worker reached its `op-*.md` and `type-*.md` files by a filename template the crawler could not see. Links are derived by crawling the prose, which cannot drift the way a declared list can; a declaration in frontmatter was considered and dropped (2026-11-05). |
| Depth of two | The sources say one level (Anthropic, agentskills.io, skill-lint). Anthropic's own repository does not hold to it for a large domain and nothing enforces it there. With shared references staying, strict one level would mean every skill links every shared file it can reach. Two by shortest path was chosen as a criterion the tree could meet with no standing failures, and it makes a separate no-cycles rule unnecessary: a file linked from SKILL.md stays at depth one however many siblings link it. |
| Links run from the specific to the general | The same layering as code: a general module does not import its callers. A shared reference that points back at a file built on it makes every reader of the general file a reader of the specific one, which is where the cycles and the eight-link chains came from. An earlier wording let a shared file describe a more specific one without its path, and that was used to get past the depth check, so the rule covers naming by description too. No published source. |
| Reachability | The audit found three orphaned references; a check of a live workspace's transcripts found no agent had read one of them for guidance since mid-September. nyosegawa lists unlinked reference files as an anti-pattern, and skill-lint and `Swival/skillscheck` flag them. |
| A skill's files have one reader | A file inside a skill is written for a reader who came through that skill's SKILL.md, and says so ("SKILL.md's step 2 sent you here"). A reader arriving from another skill lacks that context. Fixing such links one at a time kept recurring, so the rule is general: what two skills need moves to a shared reference, which forces it to be rewritten for any reader. No published source. |
| A skill is named by name, without the path to its SKILL.md | An agent finds a skill by its name through the catalog, on every harness a workspace runs. A path beside the name adds nothing, counts as a link in the checks, and goes stale when a skill moves. |
| The condition is at the link, and the file does not repeat it | The sources say each link says when to read the file. The decision to read is made at the link, so a condition at the top of the file reaches only a reader who has already decided. A file with several readers also cannot state one condition that is true for all of them. |
| Shared references stay shared; a large one splits into a core and per-path files in a directory named for the core | Shared references exist because inlined copies drift when one is changed and the rest forgotten. Everything in a shared file is read by every skill that links it, so the core holds only what every reader needs and each conditional path gets a file read only by those who take it. A directory named for the core keeps the split files beside it instead of spread through a flat directory under prefixed names. |
| A second reader has its own directory | So the files only the worker follows are told apart from the lead's by where they are. This is organization only; nothing depends on it functionally. |

### Descriptions

| Rule | Rests on |
|---|---|
| What, when, when not; triggers first | The cross-vendor core above. nyosegawa's template ends "Do not use for [important boundary]", and Anthropic's own document skills carry a "Do NOT use for" line. |
| No body content | OpenAI: procedure goes in the body. skill-creator: all "when to use" information goes in the description, not the body. superpowers gives the evidence: an agent that reads a workflow summary in a description follows it instead of reading the skill. |
| A rewrite keeps the triggers | Measured on this tree (2026-11-20, 15 trials an arm). With its steps cut from `build-app`'s description, a request to "show me a mockup" invoked the skill in 6 of 15 trials, against 15 of 15 before, and in 2 the agent built no app at all. The cut sentence was the only text in the catalog that tied a mock to that skill. A when-clause naming a mockup restored 15 of 15. |
| No first or second person | Anthropic. Neutral elsewhere. |
| A skill only another agent's task invokes says so in one line | A worker's guide is a skill so that the worker invokes it, because an invoked skill is what a compaction preserves and a file merely read is not. Its description is one line so it costs the catalog little. `disable-model-invocation` was rejected: it would block the worker's own invocation, and it is not in the spec. |
| 1 to 1,024 characters, no tighter cap | The specification. Anthropic ships 835 to 993 characters of trigger phrases, and the listing of all built-in descriptions fit its budget on a real workspace (Claude Code truncates an entry at 1,536 characters and budgets the listing at 1% of the context window). |

Every built-in description was brought to this shape, not only the flagged ones, so the tree does not ship violations of the guide it ships. Each rewrite was read against its old text for a trigger it dropped, as the row above says.

### Scripts

| Rule | Rests on |
|---|---|
| A fixed sequence is a script; run it, do not read it; no interactive prompts | Anthropic, Academy, agentskills.io, skill-creator (bundle a script when the agent keeps rewriting the same helper). |
| Takes arguments, reports through exit codes | nyosegawa: one clear job, file paths and options as arguments. |

A second motive, raised on 2026-11-17 and deferred to a later pass: every tool call a skill asks for is a model round trip, and scripting a sequence removes them.

### Old patterns

| Rule | Rests on |
|---|---|
| Version-bound text lives in one reference, out of the live path | Anthropic (an "old patterns" section) and agentskills.io (no time-sensitive information). |
| One such reference for each reader | The worker never reads the lead's file, so each reader's items are in the file that reader is sent to. |
| The step that points at an item states an observable test for whether it applies | An agent cannot reliably tell which release a workspace came from, but it can see that a script is missing or a field is absent. A test it can run decides the matter where a version would have to be inferred. The test is at the pointing step so that an agent opens the file only when it applies ("The condition is at the link"). |
| `CLEANUP:` markers | The repository's existing convention for rollout-bridging code. |

Only `update-self` got this treatment. No version floor was set, because the shims that pay to remove are tied to a release that was not yet on the stable channel.

### Paths

| Rule | Rests on |
|---|---|
| A path inside the repository must exist | nyosegawa and the support article's checklist both say to check that every referenced file exists. `system/test_python_launch_rules.py` already did this for scripts named in prose. |
| Ignored and absolute paths are not checked | One of the audit's dead-path findings was gitignored runtime state, which an existence check cannot tell from a live path, and a false positive here blocks a user's own skill. An absolute path cannot be resolved against the tree. |

### Exemptions

| Rule | Rests on |
|---|---|
| Exemptions, not warnings or counts | The end state had to be no standing warnings. A count in the test file was the first choice; in the 0.10.0 update, workspace agents had raised such counts in a built-in test file, so the record moved into each skill's own frontmatter (`metadata` is a string map in the spec and is not loaded into the catalog). |
| A failing check is fixed by restructuring, never by an exemption; a new skill carries none | Same decision (2026-11-09). |
| The reasons and who may write them are the validating repository's | The guide is mngr's, and mngr has no validator. Which reasons exist and which flow stamps them are facts about this repository only. |

A flow never restructures a user's existing skills: splitting prose into references is judgment work, unlike the scripted Python migration, so the update stamps an exemption and moves on.

### Where the rules live

| Decision | Rests on |
|---|---|
| The guide is in mngr, vendored here | People work on mngr without this repository cloned (2026-11-05). |
| A validator for what a script can decide, a review category for the rest | Not every rule can be checked deterministically (2026-11-05). |
| Vendor-neutral terms in the guide's glossary, product vocabulary in this repository's | So the guide does not overfit to one harness or one product. |

## What four harnesses keep after a compaction

Measured on 2026-11-20 in a live workspace on minds-v0.10.5, one run per harness (two for Claude Code), each on the harness's default model.
A throwaway skill carried a marker word in each of its 40 steps (a 12.5 KB `SKILL.md`), and its one reference a marker word in each of 20 items (4.9 KB).
Each chat was asked to use the skill and do its first step (read the reference), then to print about 140 KB of unrelated text through 16 shell calls, and was then compacted with the harness's own command.
It was then asked, with tools forbidden, for every marker word and for two facts from `AGENTS.md` (one 15 KB into the file, one 43 KB in), and finally to carry out step 23.

| Harness | How it loaded the skill | SKILL.md after | Reference after | `AGENTS.md` | Step 23 |
|---|---|---|---|---|---|
| Claude Code 2.1.227 | The Skill tool | Whole text, 40 of 40 | Whole text when it was among the files most recently modified, 20 of 20. After the agent wrote six newer files it was not re-attached; its words survived only because the summary copied them | Both facts | Right, from context |
| Codex 0.169.0 | `cat` in the shell | No plain text; an encrypted compaction item, from which it still gave 40 of 40 | The same, 20 of 20 | The first fact only | Wrong (the next step's word), without re-reading |
| Pi 0.87.1 | Its `read` tool | Lost, 3 of 40, and it said so | Lost, 7 of 20 | Both facts | Right, after re-reading `SKILL.md` unprompted |
| Antigravity 1.5.8 | Its `read_file` tool | 40 of 40 | 20 of 20 | The first fact only | Right, from context |

What follows from it:

- Claude Code behaved as its documentation says: an invoked skill of at most 5,000 tokens comes back whole, and a file comes back only among the five most recently modified.
- Only Claude Code has an invoked skill. The other three read `SKILL.md` as a file, so on them a skill has no more protection than any file read.
- Pi keeps the newest 20,000 tokens and a summary. Codex keeps the user's messages and an opaque item the provider wrote. Both runs were small (about 60,000 tokens), so what the summary or the opaque item carried here is an upper bound: Codex answered step 23 wrongly from it without re-reading.
- No agent invented a word. Each one's own account of what it could still see (asked as "yes, partly or no") matched what it then recalled, which is what lets the rule be conditional on it.
- Codex and Antigravity never had the end of `AGENTS.md`, compaction or not. A fresh chat on each, asked for the file's top-level headings with tools forbidden, listed them through "Using crystallized skills" (Codex, the 32,768-byte default of `project_doc_max_bytes`) and through "Communication" (Antigravity, whose rules documentation, <https://antigravity.google/docs/rules>, read 2026-11-20, cuts a rule file at 24,000 bytes; that heading starts at byte 23,334 and the next at byte 27,732).

OpenCode 1.19.5 was not measured: its agent did not launch in that workspace.
Its documentation and source say a compaction keeps a summary and a recent tail, with tool results in the tail cut short, and rebuilds the instructions from the files on each request.

## Considered and not adopted

The plan's "Decided against" list has the scope decisions. These are the published recommendations that were weighed and left out.

- **Strict one level of references.** See "Depth of two".
- **A description that says only when, never what** (superpowers). The official sources and arXiv 2607.01456 want both.
- **Word budgets of 200 to 500 words for a SKILL.md** (superpowers). Five to ten times tighter than the official guidance.
- **A description of "about 100 to 200 words", written to be "a little pushy"** (skill-creator). Skills here are not under-triggering.
- **Description length warnings at 300 or 500 characters** (arXiv 2608.08453, superpowers).
- **Gerund names across the collection** (Anthropic suggests it). Renaming churns every cross-reference and user-facing slash command. Only the two names that were wrong were changed.
- **A no-cycles rule.** Unnecessary under shortest-path depth.
- **Declaring a skill's shared references in frontmatter.** Derived by crawling instead.
- **Ratchet counts or a central allowlist for unmet criteria.** See "Exemptions".

The two arXiv studies supplied prevalence figures only; no rule rests on them.

## Not traced

No recorded reason was found for these two rules in the guide.

- A required step or contents list sits within the file's first 100 lines.
- A path outside this tree is written with a placeholder root (`<mngr>/...`, `<template>/...`).

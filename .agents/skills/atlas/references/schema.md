# Atlas page schema (operational reference)

The full design and rationale live in `references/design.md`. This file is the quick
reference the `atlas` skill enforces while generating. Where the two ever differ,
`references/design.md` wins.

## Files per topic

```
atlas/
├── topics/<slug>.toml        # declaration -- human-owned (see below)
├── <slug>.md                 # the page -- mixed ownership
└── <slug>.evidence.json      # provenance / staleness digest -- machine-owned (phase 3)
```

Machine state (checkpoint markers, cost logs) lives outside the repo under
`data/.state/atlas/<slug>/`.

## Sections -- fixed schema, in this order

| # | Section | Ownership | Budget | Content |
|---|---|---|---|---|
| 0 | Status line | machine | 1 line | Agent state, last activity, checkpoint, commits. No prose, no model -- produced by `scripts/atlas_status.py`, wrapped in `<!-- atlas:status -->` / `<!-- /atlas:status -->` markers so the checkpoint clock can refresh it in place. |
| 1 | Current state | generated | 120 w | Where it stands **today**, present tense, **no dates**. Always first. |
| 2 | Why this exists | authored | 80 w | The problem. Written once by a human; **never regenerated**. |
| 3 | How it got here | generated | 200 w | Dated milestones, newest last. The only historical section. |
| 4 | Decisions | generated + pinnable | 200 w | One line each: decision, date, rationale, citation. |
| 5 | Implementation shape | generated | 150 w | The 5-10 files that matter, and what each does. |
| 6 | Open questions | mixed | 100 w | Each with what would resolve it. |
| 7 | Next steps | mixed | 80 w | Concrete and checkable. |
| - | Sources | machine | - | Footnote definitions (`[^id]: source — "verbatim quote (<=200 chars)"`), rendered as a linked Sources list. |

Target total ~930 words. **Hard cap: fail above 1,100 words** (§1-§7 body, excluding
the footnote definitions). Over budget is a failure whose fix is to
**split the topic**, never to relax the cap.

## Projects and features (two levels)

Atlas is a book of **projects**, each holding one or more **feature** one-pagers.
A feature is a topic (its own page, `atlas/<slug>.md`); the `project` field on the
declaration groups features under one project. Different projects are different
sections/tabs; a big project accumulates several feature pages. A topic with no
`project` is its own standalone project. `atlas/index.md` (built by
`scripts/atlas_index.py`) is the browsable book: one section per project, each
listing its features with live §0 status. Optional per-project title in
`atlas/projects/<project>.toml`.

## The declaration (`atlas/topics/<slug>.toml`)

```toml
slug = "discovery-supervisor"
title = "Detached discovery supervisor"
project = "discovery"        # groups this feature under a project (tab); default: the slug
status = "proposed"          # proposed | active | paused | shipped | abandoned
started = "2026-05-14"
checkpoint_interval = "3m"   # 1m-10m
live_model = true            # opt in: auto §1/§7 refresh + end-of-task full page
full_gen_turns = 12          # optional: turns of work that mark a task "large"

[match]
branches = ["mngr/discovery-*"]
paths = ["system/apps/system_interface/**"]
agent_labels = { topic = "discovery-supervisor" }   # optional
prs = [1204]                                         # optional
commits = ["7ffb1c2576"]                             # optional
```

- `status` is **human-set and never inferred**. The skill may *report* that a
  topic looks shipped, but must not change the field.
- A topic an agent creates itself starts `proposed` and renders the
  unconfirmed-topic banner until a human flips it to `active` (decision 3).

## Validation checklist (phase 1 -- run by hand at generation time)

- [ ] All sections present, in order.
- [ ] Body within the 1,100-word hard cap.
- [ ] §1 is present tense with **no date-like tokens**.
- [ ] §3 is past tense; every entry dated; no future modals ("will", "plan to").
- [ ] Every generated sentence in §1/§3/§4/§5 carries a citation.
- [ ] Every citation resolves to a footnote definition (`[^id]: ...`), which
      carries a verbatim quote (a quoted-but-unresolvable citation is valid --
      decision 7). Citation sources: `transcript:<event_id>` (the agent's own work
      -- primary for §1/§3/§4), a commit hash, a PR number, or a file path.
- [ ] §2 and any `<!-- atlas:pinned -->` blocks are byte-for-byte preserved.
- [ ] Nothing was silently truncated -- record any drop on the page and in the sources.

Full mechanical validation (a script, secret-scan gate, staleness digest) is
phase 3; in phase 1 the skill self-checks against this list before writing.

## Pinned (human-edited) blocks

A human edit is wrapped so regeneration cannot touch it:

```markdown
<!-- atlas:pinned reason="the generated version misattributed this" -->
- **2026-05-02 -- window 0 sleeps rather than running claude.** ...
<!-- /atlas:pinned -->
```

Copy pinned blocks through verbatim. Feed them to the generator as context so it
does not contradict them, but never rewrite them. Releasing a pin is an explicit
human act.

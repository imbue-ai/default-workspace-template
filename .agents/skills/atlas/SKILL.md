---
name: atlas
description: "Generate or refresh an Atlas one-pager for a long-running topic -- a single page (current state, how it got here, decisions, shape, open questions, next steps) built from prose, commits, and PRs, so someone catching up reads one page instead of scrolling transcripts. Use when asked to write, refresh, or check an Atlas page, when starting/declaring a topic, or when checkpointing in-flight work. Invoked as /atlas <slug>."
---

# Atlas -- generate and refresh a topic one-pager

Atlas keeps a book of concise one-pagers for the large, long-running bodies of
work in this workspace. The book is two levels: **projects** (a whole app/effort,
one tab each) hold **feature** one-pagers (each large feature of that project is
its own page). One page per feature; the declaration's `project` field groups
features under a project. `scripts/atlas_index.py` builds `atlas/index.md`, the
project-grouped book. Someone returning after a break reads a page -- current
state, next steps, and a machine status line -- instead of scrolling a transcript
or spending an agent's context making it narrate itself.

**You are the summarizer.** There is no model API to call: you read the sources
and write the page directly. The full design and rationale are in `references/design.md`
(canonical); the operational schema, budgets, and validation checklist are in
`references/schema.md` -- **read that file before generating.**

**The primary source is the agent's own work, not the git branch.** A topic is a
body of work an agent did; Atlas reads that work from the agent's transcript (via
`scripts/atlas_transcript.py`) and treats git, PRs, and docs as corroborating
evidence. Movement (when the live tier is stale) and §0 both key off transcript
turns, not commits. Generation is inline (you do it here). Still later phases: the
evidence file, the staleness digest, and the scheduled sweep.

## Commands

- `/atlas <slug>` -- generate or refresh the whole page for a topic.
- `/atlas <slug> --status` -- refresh only §0, the status line. Cheap, no model.
- `/atlas <slug> --live` -- refresh only the live tier (§0, §1, §7). Use when
  checkpointing in-flight work; leaves the historical half (§3/§4/§5) untouched.
  This is the "agent upgrades when free" half of the checkpoint clock -- run it at
  a free moment (turn end, a worker returning, going WAITING), especially when the
  live-tier reminder says a topic is stale.
- `/atlas --new <slug>` -- scaffold a new topic (declaration + skeleton page).
- `/atlas <slug> --track-me` -- record the current agent on the topic so Atlas
  reads *this* agent's transcript for it (branch-free association). Resolution
  order: explicit `agent_ids` (this) -> `agent_labels` -> current agent by branch.

If no slug is given, list the topics under `atlas/topics/` and ask which.

## Activation (the skill wires its own hooks)

Atlas does **not** ship pre-wired into `.claude/settings.json`. On first use --
and idempotently on every run -- ensure its hooks are installed:

```bash
python3 .agents/skills/atlas/scripts/atlas_install_hooks.py
```

This does two things: it adds the `PostToolUse` / `Stop` / `UserPromptSubmit`
entries that drive the checkpoint clock, the live-tier reminder, the prompt
router, and the end-of-task Atlas Summary (appending only what's missing, leaving
every other hook untouched); and it **scaffolds `atlas/topics/`** so the book
exists. That second part matters -- the checkpoint/router hooks and the sweep only
act once `atlas/topics/` is present, so wiring the hooks without scaffolding the
book would leave them no-op'ing forever. Until the installer runs, nothing
auto-updates (you can still generate on demand with `/atlas <slug>`). Run it as
the first step of any Atlas work in a workspace that hasn't been wired yet.

## The status line (§0) -- always do this first, it is free

```bash
python3 .agents/skills/atlas/scripts/atlas_status.py <slug>
```

It prints one line from the declaration, `mngr list` (only if the topic declares
`agent_labels`), git, and the checkpoint marker -- no model. Put its output as the
single §0 line, directly under the title (and the unconfirmed-topic banner, if
`status = "proposed"`), **wrapped in status markers** so the checkpoint clock can
refresh it in place:

```markdown
<!-- atlas:status -->
RUNNING · last active 40s ago · checkpointed 2m ago · 3 commits since
<!-- /atlas:status -->
```

`--status` stops here. The phase-2 checkpoint hook (`scripts/atlas_checkpoint.py`)
rewrites exactly the text between these markers on the interval -- so every page
must carry them.

## Generating or refreshing the page

Read `references/schema.md` first, then:

1. **Load the declaration** `atlas/topics/<slug>.toml`. If it is missing, stop and
   offer `/atlas --new <slug>`. Note `status`, `[match]`, and `checkpoint_interval`.

2. **Preserve human-owned content.** Read the existing `<slug>.md` if present.
   Copy §2 (Why this exists) and every `<!-- atlas:pinned -->` block through
   **verbatim**. Hold them as context so the rest does not contradict them; never
   rewrite them.

3. **Gather evidence. The transcript -- the agent's own work -- comes first**;
   git/PRs/docs corroborate it:
   - **Transcript** (primary, for §1/§3/§4) -- the agent's turns: what was asked,
     what it did, what it decided and why. Pull a reduced, citation-tagged view:
     ```bash
     python3 .agents/skills/atlas/scripts/atlas_transcript.py reduce --slug <slug>
     ```
     Each kept turn is tagged `transcript:<event_id>` with its timestamp -- use
     those for §3 dates and §4 rationale. For a `--live` refresh, pass
     `--since <epoch>` (the last live refresh) to read only new work.
   - **Prose** -- `docs/`, prior Atlas pages, changelogs. Already summarized.
   - **PRs** -- `gh pr view` / `gh pr list` for PRs the declaration lists.
   - **Commits / files** -- `git log`, matched-path contents, for §5 (the shape)
     and to corroborate what the transcript claims was done.

4. **Write the sections** to the schema and budgets in `references/schema.md`.
   §1/§3/§4 come from what the agent actually said and did (the transcript); §5
   from the files. Keep §1 present-tense and dateless; keep §3 past-tense and every
   entry dated. Route uncertainty into §6, not into hedged §1 prose.

5. **Cite every generated sentence** in §1/§3/§4/§5. A citation is a footnote
   marker (`[^id]`) resolving to a **footnote definition** at the end of the page
   (`[^id]: source — "verbatim quote (<=200 chars)"`), which the viewer renders as
   a linked **Sources** list. Resolvable sources: a
   **`transcript:<event_id>`** (the agent's own words -- primary for §1/§3/§4), a
   commit hash, a PR number, or a file path. A transcript citation degrades
   gracefully: when the agent is GC'd the link dies but the quote stays valid
   (decision 7). Invent nothing you cannot cite.

6. **Validate mechanically** -- do not eyeball the checklist, run it:

   ```bash
   python3 .agents/skills/atlas/scripts/atlas_validate.py <slug>
   ```

   It enforces sections/order, the §0 markers, the 1,100-word cap, citation
   resolution, balanced pins, and a secret-scan gate (errors fail; warnings are
   advisory). Fix every error before finishing. If it flags the word cap, do not
   trim to fit -- split the topic.

7. **Write the working tree only. Never commit.** Generation edits `<slug>.md`;
   review and commit happen through the normal git flow, by a human or a later
   step -- not by this skill. After a full or `--live` regeneration, tell the
   checkpoint clock the live tier is fresh (clears the reminder, resets the
   movement baseline, records the event):

   ```bash
   python3 .agents/skills/atlas/scripts/atlas_checkpoint.py --slug <slug> --clear-live
   ```

   You do **not** need to touch §0 by hand between generations: the checkpoint
   clock (below) keeps it current on the interval. After a **full** generation
   (not `--live`), also record provenance so staleness can be computed:

   ```bash
   python3 .agents/skills/atlas/scripts/atlas_evidence.py record --slug <slug>
   ```

   Staleness ("N turns since generation") is then computed on every read and
   shown in §0 -- no assertion needed. Finally, rebuild the project book so the
   new/updated feature shows under its project:

   ```bash
   python3 .agents/skills/atlas/scripts/atlas_index.py
   ```

8. **Report** what you did in one or two lines: which tier you refreshed, the word
   count against budget, and anything you dropped or could not cite.

## Scaffolding a new topic (`/atlas --new <slug>`)

1. Write `atlas/topics/<slug>.toml` with `status = "proposed"`, today's `started`,
   a `checkpoint_interval` chosen from the topic's shape (fast/small work -> `2m`,
   ordinary -> `5m`, long grinding steps -> `10m`), and a `[match]` block.
2. Ask the human for §2 (Why this exists) -- it is authored, not generated.
3. Generate the rest as above. The page renders the unconfirmed-topic banner
   until the human sets `status = "active"`. Do **not** flip it yourself.

## The checkpoint clock (phase 2)

Pages stay current mid-task without an agent thinking about it, via the hybrid of
decisions 1-2:

- **§0, on the interval, for free.** A rate-limited `PostToolUse` hook
  (`scripts/atlas_checkpoint_hook.sh` -> `atlas_checkpoint.py`) refreshes the §0
  status line in place for every topic matching the current branch, and logs a
  checkpoint event to `data/.state/atlas/<slug>/checkpoints.jsonl`. Before the
  interval elapses it returns in a few file stats and writes nothing. It also
  fires once at turn end (the `Stop` hook). No model, ever.
- **§1/§7, richer, when the agent is free.** When the agent has done work --
  assistant **turns** in its transcript since the last refresh, not commits -- the
  clock raises a `live_pending` flag. A `UserPromptSubmit` reminder surfaces it;
  you refresh via `/atlas <slug> --live` at a convenient pause, then `--clear-live`
  resets the baseline.
- **Cost/movement is measured** (decision 6): every fire logs `turns_since_live`,
  `tokens_since_live`, and whether the live tier was pending, so the interval and a
  future token ceiling can be sized from real work data.

**Prompt-driven routing (`scripts/atlas_route.py`).** A `UserPromptSubmit` hook
spawns the router *detached* (no prompt latency). It classifies the incoming task
with one cheap call: work that belongs to an existing feature associates the
current agent with that page; a substantial *new* feature auto-creates a
`proposed` page (slug derived by the model — the user is never asked for one),
with `live_model = true` so it fills itself; anything small is ignored. A
heuristic gate and a debounce keep trivial/repeat prompts from spending a call.
Auto-created pages stay `proposed` until a human ratifies — never inferred active.

**End-of-task full generation (`scripts/atlas_generate.py`).** On the `turn_end`
fire of a `live_model` topic, the clock spawns this worker detached when **either**
the prompt router **linked this task to the topic** (regenerate regardless of how
many turns it took) **or** enough work has accrued since the last full generation
(`full_gen_turns`, default 12). The router links a task by making the topic the
agent's active topic and setting a `route_pending` flag the clock consumes at task
end -- so a page an agent worked on this task gets a fresh write-up even for a
short task, and a freshly auto-created page fills in immediately. It regenerates
§1/§3/§4/§5/§6/§7 plus resolved footnote **Sources** with the cheap model, citing
only sources from the reduced transcript (invented markers are stripped so
validation always passes). It copies §2 through verbatim and **skips any page with
`<!-- atlas:pinned -->` blocks**, leaving those to a human `/atlas`.
This is the automatic backstop so a page is never left a skeleton; a human-run
`/atlas <slug>` still produces the gold-standard, agent-written cited page.

**Idle backstop.** The clock only fires while an agent takes tool calls. A cron
entry (`/etc/cron.d/atlas-sweep` -> `run_job.sh --every 15m` -> `atlas_sweep.sh`)
sweeps every live topic when the workspace is idle: it refreshes §0 for free, and
runs topic detection only when `ATLAS_SWEEP_DETECT=1` (opt-in, since detection
spends tokens). Remove the cron file to disable.

**Fork freeze.** While this fork is a prototype it does not track upstream:
`update-self` is hard-disabled by a guard that trips on the `atlas/FROZEN`
sentinel (override: `ATLAS_ALLOW_UPDATE_SELF=1`). See `atlas/FROZEN`.

## Atlas Summary (the in-chat "what changed")

For a non-technical user who ran a long task and wants to know what changed
without asking, `scripts/atlas_summary.py` + `scripts/atlas_summary_hook.sh` post a
brief, plain-English, outcomes-only recap in chat when a large task ends.

- **Detection is book-independent:** it fires for *every* large task, tracked as an
  Atlas topic or not -- `LARGE_TASK_TURNS` assistant turns (the same bar as the
  book's end-of-task generation) since the last summary, counted from the current
  agent's own transcript, no topic scoping.
- **Delivery is the agent (design "Option A"):** a hook can't post to chat, so the
  `Stop` hook feeds a one-time nudge back to the working agent (exit 2); the agent,
  which has full context, ends its reply with a recap titled **Atlas Summary** and
  two sections -- **What changed** (plain-English outcomes) and **Open questions**
  (anything needing the user's decision before continuing, or "None"). On the next
  stop the baseline has advanced (and a 60s cooldown holds), so it does not re-fire.
  If the agent already explained the change in plain terms, the nudge tells it to
  just stop.

## Feature toggles (`scripts/atlas_config.py`)

Atlas has two independent halves; a user can run the book, the in-chat summary, or
both. Stored in `atlas/config.toml`, both default on:

- `pages` -- the one-pager book: §0 checkpoints, automatic generation, detection,
  the prompt router, and the viewer app's content.
- `summary` -- the in-chat Atlas Summary above.

```bash
python3 .agents/skills/atlas/scripts/atlas_config.py show
python3 .agents/skills/atlas/scripts/atlas_config.py disable pages     # summary-only
python3 .agents/skills/atlas/scripts/atlas_config.py enable  summary
```

When `pages` is off, the checkpoint clock, router, and detector no-op; when
`summary` is off, the Stop hook never nudges.

## Rules that matter most

- **Never infer `status`.** Report "looks shipped" if true; never change the field.
- **Never overwrite a human edit.** §2 and pinned blocks are sacrosanct.
- **Never commit**, and never silently truncate -- record any drop.

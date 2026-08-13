# Atlas

Atlas keeps a **book of one-pagers**: one page per *feature*, features grouped
under *projects*. Each page is kept current from **the agent's own transcript**
(what it said and did), not the git branch — so someone returning after a break
reads a page instead of scrolling a conversation. Atlas can also post a plain-
English **Atlas Summary** in chat when a large task ends.

**See also:** `SKILL.md` (commands and the generation procedure) ·
`references/schema.md` (the page schema and budgets) · `references/design.md` (the
canonical design and resolved decisions).

---

## The pieces

```
                          ┌─────────────────────────────────────────────┐
   agent works  ──▶  Claude Code hooks (.claude/settings.json)           │
   (tool calls)       PostToolUse ─┐   Stop ─┐        UserPromptSubmit ─┐ │
                                   ▼          ▼                          ▼ │
                        atlas_checkpoint_hook.sh            atlas_live_reminder.sh
                                   │                                     │
                                   ▼                                     ▼
                        atlas_checkpoint.py  ── reads ──▶  atlas_transcript.py
                        (the clock)                        (the agent's turns)
                          │      │                              ▲     ▲
             writes §0 ◀──┘      └─▶ spawns (opt-in)            │     │
                 │                    atlas_live_refresh.py ─────┘     │
                 ▼                    (Haiku rewrites §1/§7)           │
        atlas/<slug>.md  ◀───────────────────────────────────────────┘
        (the page)          atlas_status.py builds the §0 line
                 ▲
                 │  reads the whole book
        system/apps/atlas_book  ── sidebar: projects ▸ features + live status
        (the viewer tab)           pane: the rendered one-pager + status picker
```

- **`atlas_transcript.py`** — the foundation. Finds the agent(s) that work a
  topic (by `agent_ids`, then `agent_labels`, then current-agent-on-branch) and
  reads their Claude transcript. Two answers, both scoped to the topic by
  keyword: **how much work happened** (assistant turns/tokens since a time =
  "movement") and a **reduced, citation-tagged view** of the conversation (the
  asks, the decision-shaped turns, the latest turn) used to write the page.
- **`atlas_status.py`** — builds the **§0 status line** (no model): the agent's
  state, when it was last active, turns since the last checkpoint, and staleness.
- **`atlas_checkpoint.py` + `atlas_checkpoint_hook.sh`** — the **clock**. After a
  tool call (and at turn end) it checks: has the interval elapsed? If yes, it
  refreshes §0 in place (under a lock), logs a cheap checkpoint event, and — if
  the agent did new work — flags the live tier stale.
- **`atlas_ai.py` + `atlas_detect.py`** — a cheap-model wrapper and **automatic
  detection**: a heuristic gate ("enough new work?") then one Haiku call that
  proposes a new *feature* under a *project* for a human to ratify.
- **`atlas_route.py`** — the **prompt router**. Fired detached from
  UserPromptSubmit; classifies the incoming task as belonging to an existing page,
  a big new feature (auto-creates a `proposed` page, slug and all), or too small to
  track. Reuses `atlas_detect`'s proposal writer.
- **`atlas_live_refresh.py`** — the **live worker**. Opt-in per topic; a Haiku
  call rewrites the current-state and next-steps sections from recent work,
  under a per-topic token ceiling.
- **`atlas_generate.py`** — the **background full-page worker**. When a large task
  ends, regenerates every generated section plus resolved footnote sources with a
  cheap model, preserving §2 and skipping any page with pinned edits.
- **`atlas_summary.py` + `atlas_summary_hook.sh`** — **Atlas Summary**: when a large
  task ends, a `Stop` hook nudges the working agent to post a brief chat recap titled
  **<< Atlas Summary >>** with two sections — **What changed** (plain-English outcomes)
  and **Open questions** (each with lettered options, one marked *(recommended)* when
  the agent has a recommendation). Book-independent (fires for any large task); the
  agent writes it because a hook can't post to chat.
- **`atlas_config.py`** — the **feature toggles** (`atlas/config.toml`): `pages`
  (the book) and `summary` (the in-chat recap) can each be turned on or off.
- **`atlas_topic.py`** — owns the topic-declaration edits (setting a topic's
  lifecycle status); the viewer app calls it rather than editing the toml itself.
- **`atlas_validate.py`** — the **validator**: sections present and in order, the
  one-page word cap, every citation resolves, plus a secret-scan gate.
- **`atlas_evidence.py`** — writes a **provenance file** and computes
  **staleness** (turns since the page was last fully generated).
- **`atlas_index.py`** — builds `atlas/index.md`, the **project→feature book**.
- **`atlas_sweep.sh` + cron** — an idle **backstop** so §0 stays current even
  when no agent is taking tool calls.
- **`system/apps/atlas_book/`** — the **viewer tab**: a sidebar of projects and
  their feature pages with a live status line and a lifecycle badge, and a pane
  that renders the selected one-pager with a status picker. Polls every 30s.

---

## What happens on a tool call (the free tier)

1. The agent finishes a tool call → the **PostToolUse hook** fires
   `atlas_checkpoint.py`.
2. It reads the topic's state. **Not elapsed?** Return in a few file stats —
   nothing written. This is the common case, so it stays cheap.
3. **Elapsed?** Take the per-topic lock, re-read state, and:
   - recompute the **§0 line** from the transcript and splice it into the page;
   - count **new turns since the last live refresh**; if any, flag the live tier
     *stale* and (if the topic opted in) spawn the live worker;
   - log a checkpoint event (turns, tokens) — this is the cost/movement data.
4. At **turn end** the Stop hook does the same once more.

Nothing here calls a model — §0 is pure computation.

## What happens when a page is (re)generated (the rich tier)

1. `/atlas <slug>` (or the live worker on the clock) pulls a **reduced transcript**
   for the topic.
2. It writes the sections — current state, history, decisions, shape — from what
   the agent actually said and did, each sentence **cited** to a
   `transcript:<event_id>` (with a verbatim quote) or a commit/file.
3. `atlas_validate.py` gates it; `atlas_evidence.py` records provenance;
   `atlas_index.py` rebuilds the book.
4. Human-authored bits (§2 "Why this exists", pinned blocks) are copied through
   verbatim, never overwritten.

## Where things live

- `atlas/topics/<slug>.toml` — the declaration (human/agent): `project`,
  `status`, `checkpoint_interval`, and `[match]` rules.
- `atlas/<slug>.md` — the page; `atlas/<slug>.evidence.json` — its provenance.
- `atlas/index.md` — the generated book. `atlas/config.toml` — the feature toggles.
- `data/.state/atlas/<slug>/` — machine state (checkpoint state, cost log, lock),
  gitignored.

(Per-topic content under `atlas/` — pages, declarations, evidence, the index — is
gitignored; it is a workspace's own book, not template content.)

## Setup — what has to be in place

**Atlas ships as a skill; it wires itself on first use.** The scripts live under
`.agents/skills/atlas/`, but Atlas does not pre-edit `.claude/settings.json`.
Running `atlas_install_hooks.py` (the skill does this on first use, idempotently)
adds the PostToolUse / Stop / UserPromptSubmit entries that drive the checkpoint
clock, the live reminder, the prompt router, and the Atlas Summary — appending
only what's missing. Until then, §0 auto-updates are dormant; you can still
generate a page on demand with `/atlas <slug>`. `git`, `mngr`, and `python3` are
already in the image.

Things that *do* require a one-time action:

- **The idle backstop** — `/etc/cron.d/atlas-sweep` runs the 15-minute sweep. It is
  outside the repo (a container path), so on a **new machine** it must be re-created
  (the `atlas_sweep.sh` script is committed; the cron line is not).
- **The live tier (auto §1/§7 rewrites)** — needs (a) `live_model = true` on the
  topic (opt-in) and (b) **model access**: the workspace must be signed in / keyed so
  the cheap `claude_p`/Haiku calls succeed. It spends tokens (capped per topic/hour).
- **Automatic topic detection** — only runs in the sweep when `ATLAS_SWEEP_DETECT=1`
  is set; otherwise you run it by hand. It also spends a small model call.
- **The fork freeze** — `atlas/FROZEN` disables `update-self`. Present by default.

**Two halves, each toggleable** (`atlas/config.toml`, both on by default): the
**book** (`pages` — one-pagers, viewer, auto generation) and the **in-chat
summary** (`summary`). Run either or both — `atlas_config.py disable pages` for
summary-only, and so on.

## When it actually runs (triggers)

| Trigger | What it does | Model? |
|---|---|---|
| Every tool call (**PostToolUse**, gated by the interval) | refresh §0, record movement | no |
| **Turn end** (Stop hook) | same, once more | no |
| **You submit a prompt** (UserPromptSubmit) | print the "live tier stale" reminder; **route the task** (detached): tie it to an existing page, or auto-create a `proposed` page if it's a big new feature | routing: yes (Haiku) |
| Interval elapsed **+ movement + `live_model`** | spawn the worker to rewrite §1/§7 | yes (Haiku) |
| **A task ends** (turn end, `live_model`) | background full-page regeneration (§1–§7 + Sources) when the router **linked this task** to the topic, or when enough work has accrued since the last full page (≥ `full_gen_turns`, default 12) | yes (Haiku) |
| **A large task ends** (turn end, `summary` on) | nudge the agent to post an **Atlas Summary** in chat | no (the agent writes it) |
| **Idle sweep** (cron, 15 min) | refresh §0 for every live topic; detection only if opted in | no (detection: yes) |
| **`/atlas <slug>`** (you ask) | full generation — the agent writes every section, gold-standard citations | yes (the agent) |
| **Detection** (heuristic gate, then confirm) | propose a new feature/project for you to ratify | yes (Haiku) |

The common case — an ordinary tool call before the interval elapses — does nothing
but a few file stats. The prompt router and the end-of-task generator both run
**detached**, so they never add latency to your prompt or block the agent from
finishing. The auto-created page and its background full generation both use the
cheap model and stay `proposed` until you ratify; a human-run `/atlas` still
produces the richest, agent-written cited page.

## Before a one-pager exists — what the user provides

A page is never conjured from nothing. Before `atlas/<slug>.md` has real content:

1. **A topic must exist.** Three ways, all ending in `status = "proposed"` until you
   ratify: you declare it (`/atlas --new <slug>`); the sweep **detects** it from work
   already done; or the **prompt router** classifies an incoming task as a big new
   feature and auto-creates it (slug derived by the model — you never type one).
   Status is *never* inferred; **you ratify** `proposed → active` (from the viewer's
   status picker or by editing the declaration).
2. **§2 "Why this exists" is yours.** It is human-authored and never regenerated (a
   proposed topic gets a draft you confirm or rewrite).
3. **The topic must be associated with the agent(s) doing the work**, so Atlas reads
   the right transcript — via `agent_ids` (`/atlas <slug> --track-me`), `agent_labels`
   (`mngr create --label topic=<slug>`), or a branch match. **No association → no
   transcript → an empty page.**
4. **For automatic §1/§7 refresh:** set `live_model = true` (and have model access).
   Without it, §0 still stays current for free and you refresh the rest with `/atlas`.

## Who decides what — user vs. agent

**The agent does automatically (no prompt):**
- *When* to checkpoint §0 (the interval clock) and the default interval (shape-derived
  from the topic's recent turn rate, unless you set `checkpoint_interval`).
- Detecting movement (assistant turns since the last refresh) and flagging staleness.
- Writing/refreshing the generated sections — §1 current state, §3 history, §4
  decisions, §5 shape, §7 next steps — from the transcript, with citations.
- Which **project** a newly detected feature belongs to, and the **slug** for it.
- **Classifying an incoming task** (existing page vs. a big new feature) and
  **auto-creating** the page for a big one — but always as `proposed`.
- **Regenerating the full page** in the background when a large task ends.

**The user decides (Atlas will not do these on its own):**
- Whether a proposed topic is real: **ratify** `proposed → active`. Status is human-set.
- §2 "Why this exists," and any **pinned** edits (both survive regeneration verbatim).
- **Opting into** the live model tier (`live_model`) and any `checkpoint_interval`
  override.
- **Associating** agents with a topic (`--track-me` / labels).
- Setting the lifecycle state: `active` / `paused` / `shipped` / `abandoned`.

The dividing line: the agent keeps pages *current* and *proposes*; the human decides
what is a *topic*, why it exists, and when it is *done*.

## Two rules that shape everything

- **The signal is the agent's work, not the branch.** Movement and content come
  from the transcript; git is corroboration and a dormant-only fallback.
- **A hook can't run a model.** So the free §0 tier runs in the hook; the rich
  §1/§7 tier and the summary are either the working agent at a free moment or an
  opt-in out-of-band worker — never blocking the agent mid-turn.

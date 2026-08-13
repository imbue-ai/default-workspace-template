# Atlas

A book of concise one-pagers for the large, long-running bodies of work in a codebase.

Atlas serves **two purposes at once**, and the design only makes sense if both are held in view:

1. **A durable record** — what was done, what was decided, and why, surviving long after the agents
   and transcripts that produced it are gone.
2. **A live catch-up** — a rundown of work that is *running right now*, so someone returning after
   hours or days away can read one page and know where things stand, without scrolling a
   transcript or interrogating the agent.

The second is not a bonus feature layered on the first. It changes when pages are written: a page
is refreshed **during** the work, at convenient moments, not once at the end.

Atlas is built **entirely inside this template repo** — a skill, a page format, checkpoint hooks,
and a scheduled job. It is not a mngr plugin, and it requires no changes to `mngr` or to the
minds app.

---

## Scope and constraints

These are settled. Everything below assumes them.

| Constraint | Consequence |
|---|---|
| **Not a mngr plugin** | No `libs/mngr_atlas/`, no entry point, no PyPI package |
| **Lives in a fork of this template** | All code and pages ship with the workspace |
| **Nothing outside the fork changes** | No edits to `mngr`, none to the minds app |
| **The fork is frozen during the prototype** | `system/config/parent.toml` points at the fork; `update-self` is hard-disabled by a guard; no upstream merges |
| **Reuse existing machinery** | See the table in *Building blocks* — do not rebuild any of it |

The freeze deserves a note. A workspace is not a detached copy of the template: it stays connected
upward through `system/config/parent.toml`, which is what `update-self` pulls from. A fork inherits
that file still pointing at upstream, so leaving it alone means workspaces pull imbue's releases
directly, merging untested upstream changes into a tree that also carries Atlas — and doing it with
upstream's copy of `update-self`, which knows nothing about Atlas. Point it at the fork and stop
merging until Atlas has earned a permanent place. Because pointing at the fork is not itself a
freeze — the fork carries no `minds-v*` tag for `update-self` to target, so a run would fail or
silently fall back to `main` — the prototype enforces the freeze directly: a guard in the fork's
`update-self` exits with *"Atlas fork is frozen — update-self disabled during prototype"*. The
`minds-v*` release convention is adopted only if Atlas graduates (see §12).

---

## Resolved decisions (2026-08-11)

These settle the largest questions that §12 previously left open. Where a decision governs a
section, that section has been updated to match; this list is the canonical record of *why*, and
supersedes any earlier phrasing elsewhere in this document.

1. **Checkpointing is hybrid, not self-only.** An out-of-band worker keeps the live tier (§0, §1,
   §7) current on the clock from git, PR, and activity signals; the working agent *upgrades* §1/§7
   with its richer in-context version only at free moments it already owns (turn end, worker return,
   going WAITING). A `PostToolUse` hook cannot itself run a model, so pure self-checkpointing was not
   buildable as originally written — this dissolves the self-vs-observer question rather than picking
   a losing side. Governs §7.

2. **Section ownership splits by tier, with agent upgrade.** The worker owns §0 and the git-derived
   §1/§7; the agent overwrites §1/§7 when free; the worker writes §1/§7 only when the current version
   is older than the interval, so an agent-authored refresh is never flattened by a thinner one. A
   single `flock` on `data/.state/atlas/<slug>/lock` serializes every write — which also covers
   multiple agents sharing one topic via `agent_labels`. Governs §7.

3. **Topics: agent proposes, human ratifies.** An agent may create a topic it judges large enough,
   but it writes `status = "proposed"` with a draft §2, and the page renders banner-marked
   "unconfirmed topic" until a human flips it to `active`. This keeps "never inferred" intact — a
   human still ratifies — without the friction of no-page-until-declared, and reuses the `proposed`
   state already in the declaration schema. Governs §3 non-goal 1 and §5.

4. **Unit of work is the topic, with transcripts first-class.** A topic can be code-shaped or
   conversation-shaped; `agent_labels` and transcript matching are real match dimensions, not a
   fallback, and transcripts move up the source priority for conversation-shaped topics. One schema,
   one pipeline. Governs §5 (the declaration) and §6 (source priority).

5. **The freeze is enforced, not assumed.** `update-self` is hard-disabled in the fork with a loud
   guard; `parent.toml` is left as-is and no `minds-v*` tag convention is adopted until graduation.
   Governs *Scope and constraints*.

6. **Checkpoint cost is instrumented from day one.** Every checkpoint logs tokens in/out, whether it
   fired or skipped, and why, under `data/.state/atlas/<slug>/`. Once real numbers exist, add a
   per-topic hourly token ceiling that downgrades checkpoints to git-only (the worker tier) on
   breach. The affordability of the cadence is measured before anything expensive is built on it,
   not asserted. Governs phase 2 and the risk table.

7. **A GC'd-but-quoted citation is valid.** `mngr gc` guarantees transcript citations eventually
   stop resolving. A citation that carries its verbatim quote stays valid when its source is gone;
   only a citation that was *never* resolvable is a hard failure. Governs §8.

Two things called out during review were already handled here and stand as written: the pinned-block
format (§8, the `<!-- atlas:pinned -->` wrapper) and the shape-derived interval with a per-topic
override (§7, §12).

---

## Architecture as built (2026-08-12)

The sections below are the design rationale; this is the shipped shape, phases 0–3, on
`feature/atlas`. **Two levels:** a *project* groups *feature* one-pagers (a topic = a feature; the
declaration's `project` field groups them). **The signal is the agent's transcript, not git** — a
correction from live use.

**On disk** (committed, travels with the code):
- `atlas/topics/<slug>.toml` — declaration (human/agent): `project`, `status`, `checkpoint_interval`,
  `[match]` (`agent_ids` / `agent_labels` / `branches` / `paths` / `keywords`).
- `atlas/<slug>.md` — the page (mixed ownership); `atlas/<slug>.evidence.json` — provenance/staleness.
- `atlas/index.md` — the generated project book. Machine state lives outside git under
  `data/.state/atlas/<slug>/` (checkpoint state, cost log, lock).

**Scripts** (`.agents/skills/atlas/scripts/`):
- `atlas_transcript.py` — reads the topic's agent(s) transcript: `activity_since` (turns/tokens =
  movement) and `reduce` (citation-tagged content), both scoped to the topic by keyword;
  `resolve_agent_ids` (agent_ids → agent_labels → current-agent-by-branch) and `track-me`.
- `atlas_status.py` — the §0 status line (no model): state, last-active, turns-since, staleness.
- `atlas_checkpoint.py` (+`atlas_checkpoint_hook.sh`) — the clock: interval gate, movement,
  in-place §0 splice under a flock, cost log; spawns the live worker when opted in.
- `atlas_ai.py` + `atlas_detect.py` — cheap-model wrapper; heuristic-gated feature detection that
  proposes a `proposed` feature under a project.
- `atlas_live_refresh.py` — opt-in out-of-band Haiku refresh of §1/§7, token-ceiling'd.
- `atlas_validate.py` — mechanical validator (sections, cap, citations, pins) + secret gate.
- `atlas_evidence.py` — provenance record + computed staleness (turns since generation).
- `atlas_index.py` — builds the project→feature book; `atlas_sweep.sh` — the idle cron backstop.

**Wiring:** `.claude/settings.json` fires `atlas_checkpoint_hook.sh` on PostToolUse + Stop and
`atlas_live_reminder.sh` on UserPromptSubmit; `/etc/cron.d/atlas-sweep` runs the idle sweep; the
`update-self` freeze guard trips on `atlas/FROZEN`.

**Reading surface:** `system/apps/atlas_book/` — a read-only tab: sidebar of projects→features with
live status, a page pane rendering the selected one-pager, a 30s poll.

**Known-rough:** per-topic scoping is keyword-heuristic; the auto-refreshed live tier is less
rigorously cited than a full generation; the book is per-branch, so multiple agents' pages don't
unify until merged (see §12).

---

## 1. Problem

A long-running effort leaves its history smeared across places that are individually reasonable and
collectively unusable:

- **Agent transcripts** — the reasoning lives here, in
  `~/.mngr/agents/<id>/events/*/common_transcript/events.jsonl`. Complete, enormous, per-agent.
  A six-week effort ran across a dozen agents and nothing joins them.
- **PR threads and commits** — review discussion off in GitHub; commits tell you what, never why.
- **Prose docs** — describe the intended end state, and drift. Nothing detects when they have.
- **Chat scrollback** — where most decisions actually got made, and the least navigable of all.

There is no unit of aggregation between "one change" and "the whole workspace". So picking up an
in-flight effort means reading everything, asking whoever did it, or re-deriving a decision that
was already made and rejecting it a second time.

Agents pay this cost too, and pay it worse: a fresh agent starts with strictly less context than
the agent that stopped last week.

### The returning user

There is a second, sharper version of this problem, and it is the one that bites daily.

You start an agent on a substantial task and leave. Hours or days pass. You come back. The agent
has done a great deal — maybe it is still working, maybe it is waiting on you, maybe it went down a
path you would not have chosen. To find out, your options today are to scroll a long transcript,
ask the agent to summarize itself (spending its context on narration), or read the diff and infer.

All three are bad, and the third is the only reliable one.

What you want is a page that was **already being kept current while you were away** — so catching up
is a read, not an investigation. That requirement is what makes Atlas a live record rather than an
archival one, and it drives §7.

**Atlas is that missing unit.** One page per topic, generated from evidence that already exists,
refreshed at convenient moments while work is in flight, and short enough to read before you start.

---

## 2. Goals

1. Read one page — under a screen and a half — and know a topic's current state, how it got there,
   what was decided and why, what shape the implementation takes, what is unresolved, and what is next.
2. **Catch up on in-flight work after a break.** Return after hours or days, read the page, and know
   what the agent has been doing, where it got to, and whether it needs you — without reading a
   transcript and without spending the agent's context on a summary.
3. **Stay current while work is running.** A page is refreshed at convenient points *during* the
   work, so it is already accurate when someone comes looking. Never only at the end.
4. Reach primary evidence in one hop: a commit, a PR, a transcript range, a file.
5. Trust the page: see when it was last refreshed, from what, and whether the evidence has moved since.
6. Correct it. A human edit survives regeneration, permanently and without ceremony.
7. Let an agent read a page as easily as a human, so a fresh agent starts where the last one stopped.
8. Add no new infrastructure. Everything runs on machinery this workspace already has.

---

## 3. Non-goals

1. **Automatic (silent) topic discovery.** Topics are declared, never inferred into existence. An
   agent may *propose* one it judges large enough, but it lands as `status = "proposed"` and a human
   ratifies it to `active` (decision 3) — a proposal a human confirms is not silent inference.
   Inferring topic boundaries unattended is the hardest and least verifiable part of the problem;
   declaring or confirming one takes a minute and makes everything downstream checkable.
2. **Replacing docs or changelogs.** Atlas cites them and never edits them.
3. **A writing tool.** Atlas does not draft PRs, tickets, or specs.
4. **Cross-host history.** `find-transcripts` covers agents that ran on *this* host. Agents on other
   hosts are out of reach, and that is accepted.
5. **Multi-user permissions.** A workspace serves one person. Sharing is what git already does.
6. **Streaming or per-message updates.** Pages are refreshed at *checkpoints* — turn boundaries and
   state transitions — not continuously, and never mid-thought. A page can be minutes stale; it must
   never cost an agent its context to keep current. (This is a narrower non-goal than it looks: see
   §7, where in-flight refresh is a core requirement.)
7. **Prose quality guarantees.** Atlas enforces structure, length, and citations. Whether the summary
   is *good* is what the human edit path is for.

---

## 4. Building blocks

Every hard part of Atlas already exists in this workspace. The design is mostly a matter of not
rebuilding any of it.

| Atlas needs | Use |
|---|---|
| Something to summarize with | **the agent itself** — no model abstraction, no API client |
| Transcripts, including destroyed agents | the `find-transcripts` skill, which documents the exact paths |
| Heavy work in isolation | `launch-task` — own worktree, branch `mngr/$NAME`, runtime in `data/.tasks/launch-task/$NAME/` |
| Scheduled refresh | `system/libs/automations/run_job.sh` — durable, completion-tracked, `--every 15m|3h|7d`, catches up after downtime, retries killed runs |
| Waking an agent per run | `run_automation.sh` — singleton agent labelled `automation=<skill>` |
| The cron entry | the `manage-scheduled-tasks` skill (canonical guide) |
| Environment inside cron | `with_agent_env.sh` — cron scrubs the env; this rebuilds it |
| Review before publishing | worker commits to its branch; main merges on user approval |
| git and GitHub | both in the image (`gh` pinned and sha256-verified) |
| Storage | the workspace repo itself; `data/.state/atlas/` for machine state |

The most consequential line is the first. The original design of Atlas needed an abstraction for
invoking a model. Inside a workspace, **the agent is the summarizer** — a skill simply does the
work. That deletes an entire layer.

---

## 5. What a page is

### Location

```
atlas/
├── topics/<slug>.toml          # declaration — human-owned (carries `project`)
├── <slug>.md                   # the feature page — mixed ownership
├── <slug>.evidence.json        # provenance — machine-owned
└── index.md                    # generated project→feature book
```

Committed to the workspace repo, so pages travel with the code, review through the normal flow, and
survive every agent they were generated from being destroyed. That last property matters more than
it sounds: `mngr gc` reaps agents, and the moment you most want the page is after they are gone.

**Two levels: projects and features.** A page is one *feature*; the declaration's `project` field
groups several feature pages under one *project* (a topic with no `project` is its own standalone
project). Different projects are different sections/tabs; a big project accumulates several feature
one-pagers. `atlas_index.py` builds `atlas/index.md` grouped by project — the browsable book.

### Sections

Fixed schema. Presence, order, and budget are validated.

| # | Section | Ownership | Budget | Content |
|---|---|---|---|---|
| 0 | **Status line** | machine | 1 line | Agent state, last activity, last checkpoint. No prose, no model. |
| 1 | **Current state** | generated | 120 w | Where it stands *today*, present tense. Always first. |
| 2 | **Why this exists** | authored | 80 w | The problem. Written once by a human; never regenerated. |
| 3 | **How it got here** | generated | 200 w | Dated milestones, newest last. The only historical section. |
| 4 | **Decisions** | generated + pinnable | 200 w | One line each: decision, date, rationale, citation. |
| 5 | **Implementation shape** | generated | 150 w | The 5–10 files that matter, and what each does. |
| 6 | **Open questions** | mixed | 100 w | Each with what would resolve it. |
| 7 | **Next steps** | mixed | 80 w | Concrete and checkable. |
| — | **Evidence** | machine | — | Citation table, collapsed by default. |

≈930 words. Validation fails above 1,100. The one-page property is **mechanically enforced**, not
aspirational — and prose that outgrows the budget is a signal to split the topic, which the failure
message says explicitly.

### Three tiers of freshness

The two purposes in the opening map cleanly onto the page, because the live half and the historical
half want completely different refresh economics:

| Tier | Sections | Refreshed | Cost |
|---|---|---|---|
| **Status** | §0 | every read | **zero** — read from `mngr list` and the agent's activity file; no model call |
| **Live** | §1, §7 | **every 1–10 min** while work is running (§7) | small — two short sections, and skipped entirely when nothing moved |
| **Historical** | §3, §4, §5 | on demand, or the slow sweep | full regeneration |

This is what makes a 1–10 minute cadence affordable. Refreshing every few minutes does **not** mean
regenerating a page every few minutes — it means regenerating **two short sections**, skipping
entirely when nothing moved, while the expensive historical half sits still. A page-sized
regeneration on that interval would be indefensible; a two-section one is not.

§0 is worth dwelling on: because it needs no model, it is honest even about its own staleness. A
page nobody has refreshed in a week still truthfully reports *"WAITING · last active 3m ago · page
checkpointed 2h ago"*, which is often the entire answer a returning user needs.

Example:

```
discovery-supervisor          ACTIVE
RUNNING · last active 40s ago · checkpointed 2m ago · 3 commits since
```

### Current state vs. history

The rule most likely to be got wrong, so it is checked rather than merely stated:

- **§1 is present tense and carries no dates.** It describes the system as it is now. Anything in it
  that is no longer true is a bug.
- **§3 is past tense and every entry is dated.** It never describes the present.

Validation warns on date-like tokens in §1 and on future modals ("we will", "the plan is") in §3.

### The declaration

The one file a person writes. Everything else is derived.

```toml
slug = "discovery-supervisor"
title = "Detached discovery supervisor"
status = "active"          # proposed | active | paused | shipped | abandoned
started = "2026-05-14"
checkpoint_interval = "3m" # 1m–10m; how often the working agent refreshes the live tier

[match]
branches = ["mngr/discovery-*"]
agent_labels = { topic = "discovery-supervisor" }
paths = ["system/apps/system_interface/**"]
prs = [1204, 1231]
commits = ["7ffb1c2576"]
```

`agent_labels` is the interesting one: create agents with
`mngr create ... --label topic=<slug>` and they self-identify into a topic from then on.

---

## 6. Generating a page

### Sources, in priority order

Highest value per token first, because the context budget will bind:

1. **Existing prose** — `docs/`, prior Atlas pages, `VERSION_HISTORY.md`. Already summarized.
2. **PR bodies and review threads** — via `gh`, when the work has PRs.
3. **Commits** — `git log --first-parent` over matched branches, plus `git log -- <paths>`.
4. **Files** — current content of matched paths, for §5.
5. **Transcripts** — via `find-transcripts`. Richest overall, lowest value *per token*, and
   enormous. They earn their place only for §4 Decisions, where rationale often exists nowhere else.

Transcripts are pre-reduced before they enter context: the first user message, the last assistant
message, and messages matching decision-shaped cues (`instead`, `decided`, `won't`, `because`,
`reverted`, `turns out`). A crude heuristic whose failure mode is a decision not making the page —
not a decision being invented.

### Two paths, deliberately

**Inline** (default). The Atlas skill reads the sources and writes the page directly. Fast, cheap,
no ceremony. Correct for most refreshes.

**Delegated** (large or first-time topics). `launch-task` a worker, which does the reading and
drafting in its own worktree and commits to `mngr/atlas-<slug>`. Main merges on user approval. Use
this when a topic's evidence would blow the current context, or when the page is being created for
the first time and deserves review before it lands.

The choice is the skill's to make, stated in its output either way.

### Never truncate silently

When evidence exceeds budget, record what was dropped and why in the evidence file, and say so on
the page. Silent truncation reads as "covered everything" when it didn't.

---

## 7. Keeping pages current

A page that is only written when work finishes fails half of Atlas's purpose. The requirement is
that it is **already current when someone comes looking**, mid-task.

### Hybrid: a worker on the clock, the agent upgrading when free

**The cadence is a clock, not a turn count.** Turn boundaries alone are not enough, and the reason
is the case that matters most: an agent grinding through a single 40-minute turn hits *no* boundary
for 40 minutes. That is exactly the window in which someone wanders off and comes back — so a page
that only updates between turns is stale precisely when it is needed.

But the working agent cannot drive that clock itself. A `PostToolUse` hook is a separate shell
process; it cannot reach into the running agent's context and rewrite a section. So the clock is
driven by an out-of-band **checkpoint worker**, and the agent contributes only when it is already
free. Three moving parts (decisions 1 and 2):

- **The worker owns the live tier on the interval.** After a tool call, a rate-limited `PostToolUse`
  hook checks `data/.state/atlas/<slug>/last_checkpoint`; if the interval has elapsed *and* something
  moved, it spawns a short worker that rebuilds §0, §1, and §7 from git, PR, and activity signals
  alone. No working-agent context is spent and no thought is interrupted. Never the historical half.
- **The working agent upgrades §1/§7 when it is already free.** At turn end (`Stop` hook), when a
  `launch-task` worker returns, and on going WAITING, the agent overwrites §1/§7 with its richer
  in-context version. The worker writes §1/§7 *only* when the current version is older than the
  interval, so an agent-authored refresh is never flattened by a thinner git-derived one.
- **A single `flock` serializes writes.** Every write takes `data/.state/atlas/<slug>/lock`, so the
  worker, the agent, and any other agents sharing the topic via `agent_labels` never clobber the page
  or race the timestamp file.

The `PostToolUse` boundary is chosen because it lands after a tool call completes — a natural pause,
between actions rather than mid-reasoning. This is an established mechanism here: the template already
wires `SessionStart`, `PreToolUse`, `UserPromptSubmit`, and `Stop`, and its hooks are non-blocking by
convention (`system/scripts/claude_open_tickets_stop_nudge.sh` always exits 0). The Atlas hook is the
one new wire — `PostToolUse` is not currently wired — and follows the same non-blocking rule.

The interval is **scaled to the task**:

| Task shape | Interval |
|---|---|
| Fast-moving, many small steps, high commit rate | **1–2 min** |
| Ordinary implementation work | **3–5 min** (default) |
| Long grinding steps — big builds, long test runs, deep research | **8–10 min** |

Set per topic via `checkpoint_interval` in the declaration. The skill picks a default from the
topic's shape — matched path count, agents involved, recent commit rate — and the user can
override. Never below 1 minute, never above 10.

**Skip when nothing moved.** Even once the interval elapses, the hook compares cheap signals first:
the agent's activity file mtime, commit count on the topic's branches, worker completions. No
movement means no worker spawned and no write — just a bumped timestamp. An agent stuck on one long
build costs nothing across a dozen intervals.

### The other checkpoints still apply

The interval is the primary mechanism. These complement it, and are free:

| Checkpoint | Mechanism | Why |
|---|---|---|
| **Turn end** | `Stop` hook | Guarantees a fresh page whenever the agent stops, wherever the interval happened to land. |
| **Worker returns** | after a `launch-task` branch merges | A unit of work just completed, and its result is the news. |
| **Going WAITING** | agent state transition | The highest-value moment — an agent going WAITING is *exactly* when a returning user finds it. |
| **Backstop sweep** | `run_job.sh --every 15m` | Catches agents whose hooks never fired at all. |
| **Explicit** | the user asks, or `/atlas <slug>` | Always available. |

### The slow sweep

The historical half refreshes on the established recipe: a cron line invoking `with_agent_env.sh`
→ `run_job.sh --every 7d` → `run_automation.sh atlas`, waking a singleton agent labelled
`automation=atlas` and sending it `/atlas`. See `manage-scheduled-tasks`, the canonical guide.

`run_job.sh` is a better scheduler than anything worth building: it counts a run only once it
completes, catches up after the workspace was paused, and retries a run killed mid-flight. It
supports both cadences this design needs — `--every 15m` for the backstop, `--every 7d --at 3` for
the sweep.

### The catch-up read

The returning user's flow is a **brief read**: §0 status line, §1 current state, §7 next steps.
Three short things, answering "what has it been doing, where did it get to, does it need me?"

That is the interaction Atlas is optimizing. The full page is for understanding a topic; the brief
read is for resuming one.

### Staleness

Hash the resolved evidence set — sorted `(kind, id, content-digest)` — and store it in
`<slug>.evidence.json`. On check, re-resolve and compare.

Different digest → stale, with a concrete diff: *"6 commits, 1 PR since 2026-06-20"*. No model call,
so it is cheap enough to run on every read.

A page is also stale when the section schema version changes, or the skill's prompt changes — both
are legitimate reasons to consider every page out of date, and both are invisible without recording
them.

**Stale pages are shown, never hidden.** A stale page beats no page, provided the banner is
unmissable.

---

## 8. Trust

The failure modes, each with its defense:

**Hallucinated facts.** Every sentence in §1, §3, §4, §5 carries a citation resolving to a real
source in the evidence file. A citation that *neither* resolves *nor* carries a verbatim quote fails
validation. Because the citation set is a closed list handed to the generator, a fabricated claim
generally cannot be cited and so cannot pass. Each citation stores ≤200 verbatim characters, so a
claim can be spot-checked in place.

Transcript citations degrade gracefully, and this is deliberate (decision 7): when the agent is gone
the link dies but the quote survives, and a quoted-but-unresolvable citation stays valid. Given
`mngr gc`, that is the normal end state for transcript citations, not a failure — only a citation
that was *never* resolvable and carries no quote is rejected.

**Stale summaries.** Computed, not asserted — §7.

**Misleading status.** `status` is **never inferred**. Atlas may report that a topic *looks* shipped
(PRs merged, quiet 60 days) but must not change the field. A human sets it. The alternative —
concluding an abandoned effort is "active" because a PR is open — is exactly the failure Atlas
exists to prevent.

**Human corrections lost.** This is the load-bearing one. An edited block is wrapped:

```markdown
<!-- atlas:pinned reason="the generated version misattributed this" -->
- **2026-05-02 — window 0 sleeps rather than running claude.** A live claude there lets the user
  close it, tearing down the tmux session and supervisord with it. [^settings]
<!-- /atlas:pinned -->
```

Pinned blocks are copied through verbatim. The generator receives them as context so it does not
contradict them, and cannot rewrite them. Releasing a pin is an explicit act. If a single human edit
is ever silently overwritten, people stop editing and then stop trusting.

**Overconfidence.** Uncertainty belongs in §6 Open questions, and the skill routes it there rather
than hedging §1.

### Validation

| Check | Severity |
|---|---|
| Required sections present, in order | error |
| Page within word budget | error |
| Every citation resolves, or carries its verbatim quote | error |
| Pin markers well-formed and balanced | error |
| Frontmatter agrees with the declaration | error |
| Slug is DNS-safe | error |
| Every generated sentence in §1/§3/§4/§5 cited | error |
| Dates in §1 / future modals in §3 | warning |
| Page is stale | warning |
| No secret-scanner hits | error |

The last row matters more here than it would elsewhere: transcripts can contain secrets, and pages
are committed. Run the workspace's existing scanners over candidate output before writing, and fail
closed. Role-filtering transcripts to user/assistant already drops tool results, where credentials
most often appear — but a secret pasted into a chat message survives that filter.

---

## 9. Reading a page

Start with the plainest thing that works:

**v1 — files and chat.** Pages are markdown in the repo. The user opens them, or asks the agent,
which reads and renders them in chat. Zero UI to build.

**v2 — a tab (built).** `system/apps/atlas_book/` is a read-only viewer: a left sidebar of
projects→features with each feature's live §0 status, a pane rendering the selected one-pager's
markdown (Evidence included), and a 30-second poll. It reads `atlas/` directly and reuses
`atlas_index.gather()` + `atlas_status.build_status_line()` + markdown-it; only known slugs render.
It is per-checkout: it shows the `atlas/` book on *this* branch, so pages authored on another agent's
branch appear only after a merge (see §12).

---

## 10. Rollout

> **Implementation status (2026-08-12).** Phases 0–3 are largely built and committed on
> `feature/atlas`. Beyond the original plan, two changes landed from live use: (a) movement and
> content key off the **agent's transcript**, not git (see *Resolved decisions*), and (b) the book is
> **two-level** — a `project` field groups **feature** one-pagers under a project, browsable in the
> `atlas_book` sidebar viewer app. Built: the `atlas` skill, the status-line/checkpoint/transcript
> scripts and hooks, auto-detection (`atlas_detect`), the opt-in live worker (`atlas_live_refresh`),
> the validator (`atlas_validate`), evidence + staleness (`atlas_evidence`), the index
> (`atlas_index`), the idle sweep + cron, and the `update-self` freeze guard, with a unit-test suite.
> Known-rough: per-topic scoping is keyword-heuristic; the auto-refreshed live tier is less rigorously
> cited than a full generation. The phase text below is the original plan, kept for context.

**Phase 0 — three pages, written by hand.** No skill, no automation, no tooling. Pick three real
efforts in this workspace and write their pages manually against the schema in §5.

Then ask the only question that matters: **would you read this instead of asking someone?**

If no, fix the section schema and repeat. The schema is the highest-risk and cheapest-to-change part
of the whole design, and every line of code written before it settles is code written against a
guess. Phase 0 costs an afternoon.

**Phase 1 — the skill, and the status line.** `.agents/skills/atlas/SKILL.md` plus references for
the section schema, budgets, and citation rules. Sources: prose, commits, PRs — no transcripts yet,
they are the expensive noisy source and the other three carry most of the value. Inline generation.

Build **§0 the status line** in this phase, not later. It needs no model at all — it reads `mngr
list` and the agent's activity file — so it is nearly free, and it delivers a real slice of the
catch-up goal on day one. Even against hand-written pages from phase 0, a truthful *"RUNNING · last
active 2m ago"* is immediately useful.

**Phase 2 — the 1–10 minute checkpoint.** The rate-limited `PostToolUse` hook, the Stop hook, the
skip-when-nothing-moved check, `checkpoint_interval` with its shape-derived default, and live-tier
partial refresh of §1 and §7. This is the phase that makes Atlas a live record rather than an
archive, and where the second goal is actually met — so do not defer it behind evidence plumbing.

Validate against the real scenario: start a long task, leave for an hour, come back, and see
whether the brief read tells you what you needed. If it doesn't, the problem is the §1 prompt, and
finding that out here is far cheaper than after phases 3 and 4.

Instrument the cost from the first run — tokens per checkpoint, checkpoints per hour, and how often
skip-when-nothing-moved fires. The interval guidance in §7 is an opening bet, and this is the phase
that either confirms it or corrects it with data.

**Phase 3 — evidence and durability.** Transcripts via `find-transcripts`. Evidence file and
staleness digest. Pinned blocks and the edit-preservation path. Validation. The delegated path via
`launch-task` for large topics.

**Phase 4 — the rest of the automation.** The `--every 15m` backstop sweep, the `--every 7d`
historical sweep, and the `automation=atlas` agent. Worker-return and WAITING-transition
checkpoints.

**Phase 5 — surface, if earned.** The tab app.

Each phase is independently useful, and stopping after any of them leaves something that works.

---

## 11. Risks

| Risk | Mitigation |
|---|---|
| **Nobody declares topics**, so the corpus stays empty | The central bet. Declaration must stay trivial; `--label topic=` makes membership automatic thereafter. If reads stay near zero after phase 1, stop — do not automate discovery on top of an unread corpus. |
| **Wrong section schema** | Cheapest thing to test, hardest to fix later. Phase 0 exists solely for this. |
| **Fabricated decisions** — worse than none | Closed citation set; unresolvable citation fails; verbatim quotes for spot-checking. Residual: a correctly-cited but misread source. Human review is the backstop. |
| **Stale pages read as current** — Atlas reproducing its own problem | Computed staleness, unmissable banner, concrete diff. |
| **Secrets in committed pages** | Scanner gate before write, fail closed. Role-filter transcripts. |
| **Context exhaustion on large topics** | Priority-ordered packing, transcript pre-reduction, delegated path via `launch-task`, recorded truncations. |
| **A 1–10 min cadence taxes the agent it observes** — the live goal paid for out of the working agent's context. Still the design's biggest cost. | Largely designed out by the hybrid split (decisions 1, 2): the on-the-clock work runs in an out-of-band worker, so the working agent pays only for the richer §1/§7 upgrades at moments it is already free. Live tier is two short sections, never a page; skip-when-nothing-moved makes idle intervals free; the interval scales to the task. **Instrument from the first checkpoint** (decision 6) — tokens per checkpoint, fire/skip ratio — and once real numbers exist, cap per-topic hourly tokens, downgrading to git-only (worker tier) on breach. |
| **Checkpoint churn** — a page rewritten every few minutes, producing noisy diffs and a jittery §1 | Skip-when-nothing-moved covers most of it. Beyond that: only commit the page at turn end, and let intra-turn checkpoints write the working tree without committing — the user sees a current page, git sees one change per turn. |
| **Interval mis-set** — 1 min on a slow task burns tokens; 10 min on a fast one leaves the page stale exactly when it matters | Default from topic shape rather than a fixed constant. Make it trivially overridable per topic. Revisit once real usage shows how far the heuristic is off. |
| **Atlas becomes another chore** | Pages are optional and additive. Nothing breaks when the corpus is empty. Atlas must never gate unrelated work. |

---

## 12. Open questions

**What codebase does Atlas document?** *Resolved (decision 4).* Neither exclusively — the unit is
the topic, which may be code-shaped or conversation-shaped, and transcripts are a first-class
evidence source, not a fallback. `agent_labels` and transcript matching are real match dimensions.

**Are these the right seven sections?** Specifically: does "Implementation shape" earn its budget
when the code is right there? Should "Decisions" be a separate document that pages reference?

**Who declares a topic, and when?** *Resolved (decision 3).* An agent proposes a topic
(`status = "proposed"`, draft §2, banner-marked) and a human ratifies it to `active`. This keeps
"never inferred" intact while allowing retroactive, low-friction creation; declaration should still
be good at bootstrapping from an existing branch or PR set. *Still open within this:* the exact
rules an agent uses to judge a run of work "large enough to propose."

**Does the working agent checkpoint its own page, or does a separate agent watch from outside?**
*Resolved (decision 1).* Neither alone — the mechanism is hybrid. A `PostToolUse` hook cannot run a
model, so an out-of-band worker keeps the live tier current on the clock, and the working agent
upgrades §1/§7 only at free moments it already owns. This gets the returning-user guarantee without
taxing the working agent mid-turn.

**How should the interval be derived?** The 1–10 minute range is settled; how the default is picked
inside it is not. Options: static per topic, derived from topic shape at declaration time, or
adaptive — tightening while commits land quickly and relaxing during a long build. Adaptive is
most likely right and most likely over-engineered for v1. Start with shape-derived plus an
override, and let phase 2's instrumentation say whether adaptation earns its complexity.

**If Atlas graduates, does the fork live on?** Keep the fork and merge upstream forever (adopting
the `minds-v*` release convention so `update-self` has a target), or upstream Atlas itself via
`submit-upstream-changes` and stop maintaining a fork. The second is less long-run work and the
better outcome if Atlas turns out to be broadly useful.

**How do multiple agents share one book?** *Open — the current gap.* The book is files in `atlas/`
on a git branch, and each agent works in its own worktree/branch, so pages authored by different
agents don't unify until their branches merge; the viewer shows only the current checkout. Options:
(a) merge-model — pages land in the shared tree when a branch merges (works today, but delayed);
(b) shared-location model — pages write to one path all agents/viewers read, so every agent's book
shows up live. (b) is the real fix for the multi-agent case.

**Per-topic scoping is a heuristic.** Movement and content are scoped to a topic by keyword match
against the declaration (slug/title/paths/`keywords`). Generic slugs over-match and code-heavy work
is under-captured (the transcript reduction keeps prose, not tool/file-write content). A path/tool-
based attribution would be sharper.

---

## Notes

`blueprint` is forward-looking — interactive Q&A to write an implementation plan before work
starts. Atlas is backward-looking. They do not overlap; blueprint's output is a *source* Atlas
cites.

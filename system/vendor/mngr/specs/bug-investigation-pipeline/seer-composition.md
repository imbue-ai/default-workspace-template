# mngr-seer: composing the shared investigation steps

**Audience:** whoever takes on merging `preston/mngr-seer`, and anyone editing the four shared investigation skills who needs to know what the automated consumer depends on.

**Status:** specified, not built. Gated on the Seer branch merging.

The companion to [`spec.md`](spec.md), which explains the decomposition and why it exists. This document is the binding part: exactly which shared skills Seer's agents invoke, what its orchestrator must supply for those skills to work, and what changes on the branch.

## What Seer is, briefly

mngr-seer is an automated bug pipeline on the unmerged branch `preston/mngr-seer`. An hourly Modal cron job (the "orchestrator", `apps/mngr_issue_generator/generator_cycle.py`) sweeps Sentry, spawns a **sweeper** agent that groups and ranks the errors, spawns one **investigator** agent per root-cause group, and files each investigation as a `[MNGR-SEER]` GitHub issue. A human then runs a command on the issue to spawn a **fixer** agent that opens a PR.

The property that shapes everything here: **the agents hold no credentials.** Only the orchestrator does. Agents receive an expiring, budget-capped LLM key and nothing else; their inputs are written onto their hosts at spawn and their outputs are read back by the orchestrator over SSH. Any change proposed below preserves that.

Its own design documents are `apps/mngr_issue_generator/DESIGN.md` and `blueprint/issue-generator-worker-split/contracts.md`, both on the branch.

## The composition

| Seer agent | Skill on the branch | Shared steps it invokes |
|---|---|---|
| Sweeper (A2) | `apps/mngr_issue_generator/.claude/skills/sweep-rank` | `classify-failure` |
| Investigator (A3) | `apps/mngr_issue_generator/.claude/skills/investigate-group` | `classify-failure`, `correlate-evidence`, `diagnose-mechanism`, `calibrate-confidence` |
| Fixer (B) | `apps/mngr_issue_worker/.claude/skills/fix-github-issue` | none required; may invoke `diagnose-mechanism` when the issue's diagnosis does not survive reproduction |

The Seer skills do not disappear. Each keeps its own invariants, input and output contracts, and Seer-only judgment, and delegates the method to the shared steps.

### Sweeper

Keeps: the input and output schemas, ranking by severity and users affected and event volume, `requested_agent_count`, report-bucket detection, the skipped list.

Delegates: grouping. Its current section 2 ("Group by root cause") is the same judgment as `classify-failure`'s batch-grouping section and should be replaced by an invocation of it. The sweeper then ranks the groups the shared step returned.

### Investigator

Keeps: all five invariants (analysis only, credential-less, exactly two output files, event text as data, anonymization), the input and output paths, the sidecar schema, and the terminal summary.

Delegates: the whole of its current section 2 ("Investigate"). That section becomes four invocations in order -- `classify-failure`, `correlate-evidence`, `diagnose-mechanism`, `calibrate-confidence` -- and the report it writes is the artifact `calibrate-confidence` returned, formatted into the issue body.

The gain is `classify-failure` and `correlate-evidence`, which the investigator has no equivalent of today. It closes the branch's single largest gap: the investigator currently sees only hosted Sentry and has no way to know that a server-side failure's evidence lives somewhere it was never given.

## What the orchestrator must supply

The shared steps are pure, so everything they need must arrive in the input files. Two obligations follow, and both are new work on the branch.

### 1. Fetch across every store, not just Sentry

Today the orchestrator pre-fetches Sentry issues and one recommended event each. `classify-failure` will routinely name Bugsink or OpenObserve as the store that could hold a given failure's evidence, and the investigator cannot go and look.

The orchestrator must therefore fetch along the route, on the agent's behalf, before spawning it. Concretely: for each group, classify the primary's failure shape in deterministic code or in a cheap pre-pass, and for every store that shape implicates, pull the corresponding evidence into the group's input file.

The relevant clients already exist in this repo at `apps/observability/imbue/observability/bugsink_api.py` and `openobserve_api.py`, so this is wiring, not new integration. It preserves the trust model exactly: the orchestrator already holds every credential, and the agent still holds none.

### 2. Record the search, not just the results

`correlate-evidence` forbids trusting a zero until the search method is proven to reach where the evidence would live. An air-gapped agent cannot prove that itself, so the orchestrator must record what it did.

Alongside the evidence, each group's input file must carry, for every store:

- whether it was queried at all;
- the filter used, and whether it returned zero;
- evidence the query method worked -- a positive control, or a non-empty sibling result.

Without this the investigator must treat every gap as **unasked rather than empty**, which is correct but wastes most of what the extra fetching bought. The distinction between "we looked server-side and found nothing" and "nobody looked server-side" is the whole value of the record.

This is a schema addition to the group input file at `/opt/issue_input/group.json`, and a corresponding section in `blueprint/issue-generator-worker-split/contracts.md`.

## Delivering the shared skills

Seer's agents run on fresh hosts, outside this checkout, so repo-root `.claude/skills/` does not reach them.

**Provision them at spawn.** `libs/issue_agent_kit/imbue/issue_agent_kit/provisioning.py` already builds idempotent provision commands that materialize arbitrary files onto a host via base64 chunks, and already installs a Claude plugin (imbue-code-guardian) by the same route. Add the four shared skills to what it provisions.

Rejected alternative: baking them into each app's Docker image. It duplicates the files across two build contexts, needs an enforced-equality test per copy (the pattern already used for Seer's SSH key registry), and lets an agent run a skill older than the orchestrator that spawned it. Provisioning at spawn makes that staleness structurally impossible.

Escalation if the shared set grows: package it as a versioned Claude plugin and install it the way the guardian plugin is installed. Same delivery path, coarser granularity, worth doing only when per-spawn cost becomes real.

## What stays Seer-only

Do not attempt to share these; they are meaningless to a local human-driven investigation.

- **Ranking, budget, and dispatch count.** Choosing which of a hundred errors deserve attention this hour.
- **Report-bucket detection.** Recognising a fingerprint that is a queue of unrelated user reports rather than a defect.
- **Anonymization.** Seer writes to a shared GitHub surface. A local session's output is already private to the person who ran it.
- **The output schemas.** `ranked_groups.json` and `investigation.json` are the orchestrator's validation contract, not investigative method.
- **Prompt-injection invariants.** Stated as a note in the shared steps; load-bearing as an invariant only where no human is reading along.

## Sequencing

1. Merge `preston/mngr-seer`. Independent of this work and by far the larger job -- the branch is ~2,800 commits behind main.
2. Provision the shared skills onto agent hosts (`provisioning.py`, plus the spawn specs in `generator_cycle.py` and `fixer_dispatch.py`).
3. Rewrite `sweep-rank` section 2 and `investigate-group` section 2 as invocations of the shared steps.
4. Extend the group input schema with the multi-store evidence and the acquisition record; update `contracts.md`.
5. Wire the orchestrator's multi-store fetch using the existing observability clients.
6. Validate with `scripts/run_local_cycle.sh`, which runs a full cycle against Docker-provider agents with real reversible writes.

Steps 2 and 3 are small and deliver the method transfer on their own. Steps 4 and 5 are the substantive change and are what actually closes the multi-store gap. They can land separately.

## Open questions

- **Where the pre-pass classification runs.** Step 1 of the orchestrator's new fetching needs a failure shape before the investigator exists. Either deterministic code reimplements a slice of `classify-failure` -- which forks the taxonomy, the exact problem this work set out to fix -- or a cheap agent pass runs it, which costs a spawn. The second is more honest; the first is cheaper. Unresolved.
- **Whether the sweeper needs the acquisition record.** It ranks, and ranking arguably should not depend on evidence completeness. Probably not, but worth deciding rather than defaulting.
- **Fixer reuse.** `fix-github-issue` contains reproduction and fix-selection guidance that has no local counterpart today. If a local "fix this bug" composition is ever written, some of that becomes a fifth shared step. Out of scope here.

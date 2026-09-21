# Bug investigation as a composable pipeline

**Audience:** developers working on the repo-root bug-investigation skills (`.claude/skills/investigate-bug`, `.claude/skills/access-*`) and on the mngr-seer automated issue pipeline (`apps/mngr_issue_generator`, `apps/mngr_issue_worker`, `libs/issue_agent_kit` — currently unmerged, on branch `preston/mngr-seer`).

**Status:** design agreed. The decomposition and the local composition are implemented on this branch; the Seer-side composition is specified here and in [`seer-composition.md`](seer-composition.md) but not built.

Two systems in this repo investigate the same bugs, against the same evidence, and share nothing:

- **`/investigate-bug`** — local, agentic, human-driven, privileged bug investigation. One bug at a time, a human in the loop, full credentials, live queries.
- **mngr-seer** — automated, sandboxed, locked-down, batched, hands-off bug fixing that is still human-reviewed. Many bugs an hour, no human until the GitHub issue is read.

They are not variants of one another and should not be merged. They are two *consumers* of one investigative method. This spec names that method's steps, extracts each into a skill both can invoke, and defines the seam that lets a privileged interactive agent and an air-gapped batch agent run the same reasoning over evidence they obtained in completely different ways.

The payoff is the one that motivates the work: an improvement to any step — a sharper failure taxonomy, a better absence check, a more honest confidence gate — lands for both consumers at once.

Related:

- [`seer-composition.md`](seer-composition.md): the binding contract for the automated consumer — which skills Seer's agents invoke, what the orchestrator must supply, and what changes on `preston/mngr-seer`.
- `.claude/skills/investigate-bug/SKILL.md`: the local composition, rewritten on this branch as a router over the shared steps.
- `apps/mngr_issue_generator/DESIGN.md` (on `preston/mngr-seer`): Seer's trust boundary and cycle mechanics.

## Contents

- [Background](#background)
- [Goals and non-goals](#goals-and-non-goals)
- [The two consumers](#the-two-consumers)
- [The seam](#the-seam)
- [The decomposition](#the-decomposition)
- [Why skills, not reference documents](#why-skills-not-reference-documents)
- [Getting shared skills into a sandbox](#getting-shared-skills-into-a-sandbox)
- [Composition: the local consumer](#composition-the-local-consumer)
- [Composition: the automated consumer](#composition-the-automated-consumer)
- [What is deliberately not shared](#what-is-deliberately-not-shared)
- [Failure modes](#failure-modes)
- [Alternatives considered](#alternatives-considered)
- [Testing](#testing)
- [Effort](#effort)

## Background

`/investigate-bug` and its `access-*` companions landed in September 2026. Seer was written in July and August 2026 and has never merged. They were built independently, and it shows: each is strong exactly where the other is weak.

`/investigate-bug` is thick with domain knowledge and evidence-acquisition technique. It knows that `[Errno 8] nodename nor servname provided` means no packet ever left the user's machine, that a Bugsink `event_id` in an `internal_error` body is the one direct bridge from a client-visible 500 to the server exception, and that the client-side `anonymous_user_id` and the server-side SuperTokens id never meet. It knows to run a positive control before believing a zero. What it has no notion of is what to *produce* when the investigation does not reach a root cause. It drives to a conclusion or it trails off.

Seer's `investigate-group` is the mirror image. Its investigative method is four short paragraphs and it has no domain knowledge at all — no failure taxonomy, no store map, no correlation keys. What it has instead is process: a confidence gate that is schema-enforced (a diagnosis may only propose fixes when it declares high confidence), a defined artifact for the failure case (`needs-logs`: name the exact logging that would settle the question), and a feedback loop that turns that artifact into a future solved case. It also cannot see past hosted Sentry — Bugsink and OpenObserve do not exist in its world, though both were live in the repo when it was written.

Neither gap is fundamental. Both are consequences of the two systems never having had a shared vocabulary for the steps of an investigation.

## Goals and non-goals

Goals:

- Name the steps of a bug investigation, and make each one a skill that either consumer can invoke.
- Rewrite `/investigate-bug` as a composition of those steps rather than a monolith, so that reading it tells you the shape of the method rather than all of its details at once.
- Give Seer a set of pieces it can compose into its pipeline without weakening its trust model.
- Transfer, in both directions, the capability each side is missing: multi-store awareness to Seer, a defined failure artifact to the local consumer.
- Keep the shared steps free of anything that assumes a human is present, or that credentials exist.

Non-goals:

- Merging the two consumers. They have opposed constraints and both are correct for their setting.
- Changing what the `access-*` skills do. They are already the right shape and this spec builds on them.
- Landing Seer. That branch is 2,808 commits behind main and its merge is separate work. This spec only ensures the pieces are there when someone takes it on.
- Building the shared-skill delivery mechanism. It is specified here and scheduled with the Seer work, because only Seer needs it.

## The two consumers

| | `/investigate-bug` (local) | Seer (automated) |
|---|---|---|
| Driven by | A human, interactively | An hourly cron cycle |
| Trust posture | Privileged: latchkey, Vault grants, SSH tunnels | Air-gapped: one expiring LLM key, nothing else |
| May execute code | Yes — `dig`, `curl`, `minds-admin`, tunnels | No (investigator); yes in a sandbox (fixer) |
| Evidence reach | All stores, live, adaptively | Whatever the orchestrator pre-fetched |
| Input | One reported failure, freeform | A ranked batch of Sentry issues |
| Output | Prose to the human in session | Schema-validated files that become a GitHub issue |
| Review | Continuous — the human is watching | Deferred — a human reads the issue, then the PR |
| Failure of the investigation | The human redirects | Must produce a useful artifact anyway |

The row that matters most is trust posture. It is why a naive extraction fails: any shared step that says "run `latchkey curl ...`" is unusable by Seer, whose entire security value is that its agents hold no credentials. The decomposition has to put that difference somewhere specific rather than letting it contaminate every step.

## The seam

Split an investigation into three phases by *who can do them*:

```
  ACQUISITION            INTERPRETATION              EMISSION
  (privileged,           (pure, shared)              (consumer-specific)
   consumer-specific)

  access-sentry      ┐                            ┌ investigate-bug
  access-bugsink     │                            │   -> prose, in session
  access-openobserve ├─> evidence ─> classify-failure   │
  access-analytics   │    bundle      correlate-evidence├ investigate-group
  (Seer: a bundle    │                diagnose-mechanism│   -> report.md + sidecar.json
   pushed onto the   ┘                calibrate-confidence
   host at spawn)
```

**Acquisition is privileged and differs per consumer.** Locally it means running live queries against Sentry, Bugsink, and OpenObserve, adaptively, following the evidence. For Seer it means the trusted orchestrator fetching the same evidence and materializing it onto the sandbox before the agent starts. These are genuinely different activities and they stay separate.

**Interpretation is pure and shared.** Given evidence, the reasoning that turns it into a named failure mechanism with a calibrated confidence does not depend on how the evidence arrived. This is the whole reusable core, and it is the part that benefits from improvement.

**Emission is consumer-specific.** A human in session wants a narrative. An orchestrator wants two files it can schema-validate and refuse.

The interface between acquisition and interpretation is an **evidence bundle**: the set of events, logs, source, and metadata the interpretation steps read. It is not a new file format. It is a contract about *what must be present* before interpretation can begin, so that a step can state its inputs without caring whether they were fetched a second ago or pushed an hour ago.

This is the move that makes Seer's air-gap tractable. Seer does not need an exception to the shared steps. Seer's orchestrator is simply another acquisition adapter — one whose "query" happens ahead of time, in trusted code, and whose result is a file. The interpretation skills neither know nor care.

## The decomposition

Four shared skills, each pure — no credentials, no network, no execution required.

### `classify-failure`

*Read a failure's text and decide what kind of failure it is and where its evidence can possibly live.*

Owns the failure-shape taxonomy (`getaddrinfo` failures vs `connection refused` vs a wrapped subprocess error vs an `internal_error` carrying an event id) and the evidence-boundary table — which store observes what, and crucially what each store *can never* hold. Its central question is the one `/investigate-bug` already opens with: did the failing operation's request ever leave the user's machine?

Both consumers need it. The local agent uses it to decide where to look. Seer's sweeper uses it to group by failure mechanism rather than by message text, and Seer's investigator uses it to know whether the Sentry-only evidence it was handed could even contain the answer.

### `correlate-evidence`

*Join evidence across stores, and decide what an absence means.*

Owns the correlation keys (Bugsink `event_id`, deploy id, environment, the two identity spaces and the fact that they never meet server-side), the bridging moves available when identity fails, and the discipline of interpreting a zero: run a positive control first, distinguish consent-gated silence from genuine absence, mind retention windows and tier.

The absence discipline is the most valuable thing in the current `/investigate-bug` and the thing Seer most conspicuously lacks. Seer's investigator, air-gapped, cannot run a positive control itself — so the skill states what the acquisition layer must record for absence to be interpretable at all, which becomes a requirement on Seer's orchestrator rather than a capability its agent has to fake.

### `diagnose-mechanism`

*Turn evidence plus source into a named failure mechanism, with honest alternatives.*

Owns the method: read the failure (exception chain, failing frame and neighbours, breadcrumbs, and how grouped instances differ from each other — divergence is often the best clue to a shared cause), read the code at the erroring release, use `git log` and `git blame` across the span between that release and current main to see whether a fix already landed, then name the mechanism and the competing hypotheses the evidence does not yet separate.

This is Seer's contribution. `/investigate-bug` currently routes to evidence and then leaves "drive to root cause" as an instruction rather than a method.

### `calibrate-confidence`

*Decide whether a diagnosis is actionable, or name the observability that would make it so.*

Owns the gate. High confidence means you can name the defect precisely — file, lines, triggering conditions — and would bet that a fix written from your description lands. Anything less is not a weaker fix proposal; it is a different artifact: the specific log lines, call sites, and fields that would separate the surviving hypotheses, described well enough to implement directly.

The reason to make this a step rather than a flourish is that it is the only defined output for an investigation that did not succeed. Seer already treats it as one and enforces the coupling in a schema. Locally it is currently absent, which is why a local investigation that stalls produces nothing durable.

## Why skills, not reference documents

Both would remove the duplication. Skills are the right choice because **a skill is obligatory and a reference document is voluntary.** An agent that invokes `calibrate-confidence` has loaded its contents and is operating under them. An agent pointed at `docs/calibration.md` may read it, may skim it, or may decide it already knows. For steps whose entire purpose is to impose a discipline the model would otherwise skip — run a positive control before believing a zero; do not claim high confidence to avoid writing a needs-logs report — voluntary reading defeats the point.

Reference documents still have a place for material that is looked up rather than obeyed: long tables, per-store endpoint inventories, credential locations. Where a shared step needs that kind of bulk, it lives in a `reference.md` beside the `SKILL.md` and the skill cites it. The rule is: anything that changes the agent's behaviour is in the skill; anything the agent looks up mid-task can be a reference.

## Getting shared skills into a sandbox

Repo-root `.claude/skills/` is loaded automatically for an agent working in this checkout. Seer's agents are not in this checkout — they run on fresh hosts whose Docker build context is a single app directory, which is why each Seer app carries its own `.claude/skills/`.

The repo's existing answer to this shape of problem is duplication with an enforced-equality test: Seer's worker ships a byte-identical copy of the generator's SSH key registry precisely because the build contexts are separate. That works but does not scale past a couple of files, and it drifts silently between the moment someone edits one copy and the moment CI catches it.

**The recommendation is to provision shared skills onto the agent host at spawn, not to bake them into an image.** `libs/issue_agent_kit/imbue/issue_agent_kit/provisioning.py` already builds idempotent provision commands that materialize arbitrary files onto a host via base64 chunks, and already installs a Claude plugin (imbue-code-guardian) the same way. Shared skills are just more files through a mechanism that exists.

This is better than baking for three reasons. Version coherence: the agent gets the skills from the same commit as the orchestrator that spawned it, not from whenever the image was last built. No duplication: one copy in the repo, no equality tests to maintain. Uniformity: local Docker-provider test runs get exactly what production gets.

If the shared set grows large enough that per-spawn provisioning is wasteful, the escalation is to package it as a versioned Claude plugin and install it the way the guardian plugin is installed — same delivery path, coarser granularity. That is a later optimisation, not a prerequisite.

The work is scheduled with the Seer merge because Seer is the only consumer that needs it. Nothing on this branch depends on it.

## Composition: the local consumer

`/investigate-bug` becomes a router: the flow through the steps, the decisions between them, and the local-specific parts that belong to no shared step.

```
report arrives (screenshot, pasted error, "user X hit Y")
        |
        v
  classify-failure  ---------> failure shape + which stores can hold evidence
        |
        v
  acquire, live:  access-sentry / access-bugsink / access-openobserve
        |          (+ the bug-report move when the evidence is on the user's machine)
        v
  correlate-evidence  -------> joined timeline; absence interpreted, not assumed
        |
        v
  diagnose-mechanism  -------> mechanism + surviving hypotheses
        |
        v
  calibrate-confidence  -----> actionable diagnosis, OR the logs that would settle it
        |
        v
  report to the human in session
```

What stays in `investigate-bug` itself: the live server-health probe, the bug-report move (asking a user to file an in-app report, which is an interaction no automated consumer can perform), the access-boundary notes about Vault grants and tunnels, and the living-document protocol for filing corrections against skill rot.

The visible change for a local user is that a stalled investigation now has somewhere to land. Previously it ended in conversation. Now `calibrate-confidence` turns it into a concrete observability proposal, which is the same artifact Seer produces and can be handled the same way.

## Composition: the automated consumer

Specified in full in [`seer-composition.md`](seer-composition.md). In outline:

- Seer's **sweeper** (`sweep-rank`) invokes `classify-failure` to group by failure mechanism, and keeps its own ranking, budget, and report-bucket logic, which are Seer-only concerns.
- Seer's **investigator** (`investigate-group`) invokes `classify-failure`, `correlate-evidence`, `diagnose-mechanism`, and `calibrate-confidence`, and keeps its own invariants, output schema, and anonymization rules.
- Seer's **orchestrator** becomes an acquisition adapter: it must pre-fetch across every store `classify-failure` names as possibly-relevant, not just Sentry, and it must record what it searched and found empty so that `correlate-evidence` can distinguish a real absence from an unasked question.

That last requirement is the substantive change to Seer, and it is what closes its multi-store gap without touching its trust model. The agent still holds no credentials. The orchestrator, which already holds all of them, looks in more places.

## What is deliberately not shared

- **Ranking, batching, and budget.** Deciding which of a hundred errors deserve attention this hour is meaningless to a consumer handed one bug by a human.
- **Report-bucket detection.** Recognising that a fingerprint is a queue of unrelated user reports rather than a defect matters when sweeping a project; a human who filed one bug has already done this.
- **Anonymization.** Seer writes to a shared GitHub surface and must sanitize. A local investigation is a privileged session whose output the human already sees. Forcing local prose through Seer's anonymization rules would degrade it for no gain.
- **Prompt-injection posture.** Seer's agents are unsupervised and process untrusted error text, so the rule that Sentry text is data and never instruction is load-bearing there. It is stated in the shared steps as a note rather than an invariant, because locally a human is reading along.
- **Reproduction.** Seer defers it to the fixer by design; locally it is often the first thing a human does. Neither belongs in a shared interpretation step.

## Failure modes

**The shared steps drift toward one consumer.** Whoever edits them most shapes them. Mitigation: each shared skill states its inputs abstractly (an evidence bundle, a source tree) rather than naming a command that produces them, and neither consumer's vocabulary appears in a shared step.

**A shared step silently assumes credentials.** The most likely regression, and the one that breaks Seer. Mitigation: shared steps never contain an executable command against a credentialed service. Acquisition verbs live in `access-*` skills only.

**Skill sprawl.** Four shared steps plus four access skills plus two compositions is already a lot to hold. Mitigation: the shared four map to four questions — what kind of failure, does the evidence join, what is the mechanism, how sure am I — and the composition skills exist precisely so no one has to hold the rest.

**Seer's copy goes stale.** Whatever delivery mechanism is chosen, a Seer agent could run an old skill. Mitigation: provision-at-spawn makes staleness structurally impossible; baking into an image does not, which is the main argument against baking.

**The decomposition outruns its only user.** Seer is unmerged. If it never lands, four shared skills serve one consumer and the indirection is a cost with no benefit. Mitigation: the rewrite is a net improvement to `/investigate-bug` on its own terms — it gains a method for diagnosis and a defined failure artifact it does not currently have. The extraction pays for itself before Seer arrives.

## Alternatives considered

**Leave them separate and copy improvements by hand.** Rejected: it is what produced the current asymmetry. Neither side's improvements have ever reached the other.

**One skill with a mode flag** (`--sandboxed`). Rejected: it complects the two trust postures into one document where every paragraph needs a caveat, and it puts privileged commands in front of an agent that must not run them.

**Shared reference documents, thin skills per consumer.** Rejected on the obligation argument above. Retained for bulk lookup material only.

**Share by extracting a Python library rather than skills.** Rejected: the reusable substance here is judgment, not computation. The parts that are computation (fetching, parsing, schema validation) are already in `libs/issue_agent_kit` on Seer's side and in latchkey on the local side.

**Fold the four steps into two** (acquisition-adjacent, reasoning). Rejected: `calibrate-confidence` in particular earns separation because it is the only step with a defined output for failure, and both consumers should be able to invoke it against a diagnosis they arrived at by any route.

## Testing

The shared steps are prose, and prose is tested by use. Concretely:

- **Local:** run `/investigate-bug` against a real reported failure and confirm the composition reaches the same conclusion the monolith would have, and that a deliberately under-determined case produces a `needs-logs`-shaped proposal rather than trailing off.
- **Seer:** `scripts/run_local_cycle.sh` on the Seer branch runs a full cycle with Docker-provider agents and real reversible writes. A cycle whose investigators invoke the shared steps and produce valid sidecars is the acceptance test.
- **Mechanical:** the enforced-equality test pattern already used for Seer's SSH registry applies to any duplicated skill copy, if provisioning-at-spawn is not adopted.

There is no unit test for a skill and none should be invented. The `test_ratchets.py` mechanism is for code anti-patterns, not prose.

## Effort

The decomposition and local rewrite are this branch. The Seer side is gated on that branch merging and is estimated in [`seer-composition.md`](seer-composition.md).

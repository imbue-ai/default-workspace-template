# Goal-driven conversation turns

## Purpose and scope

This spec extends the minds_evals driver so that a `prompts` entry can expand into a bounded multi-exchange back-and-forth: a goal-holding client LLM keeps replying until it is satisfied or its budget runs out.
This is the hand-rolled alternative to harbor's simulated-user feature; the competing option, a harbor fork composing simulated-user trials with multi-step tasks, is described in imbue-ai/mngr-internal PR #757.
The audience is the engineer implementing the change and the maintainers of the eval corpus.
Out of scope: any harbor change, adopting harbor `[[steps]]` (see "Interaction with multi-step tasks"), client tool use, and in-conversation verification.

## Background

Today one `prompts` entry is exactly one conversation turn: the loop in `driver.py::_run_conversation` iterates once per entry, and a `TurnSource` produces one message per entry (`LiteralTurnSource` returns the string verbatim; `PersonaLLMTurnSource` makes one decider call).
[concise.md](concise.md) reserves `TurnSource` as the extension seam for richer sources, and records three invariants this spec preserves: the loop owns all environment I/O, sources never touch the environment, and all nondeterminism is confined to the LLM-backed sources.
The budgeted host-side LLM loop already exists in this codebase as the ui_flows read-decide-act loop (`ui_flows.py`, `evidence_collection.py::_run_one_flow`): forced tool calls on the same Anthropic plumbing as the decider, with a hard step budget and deadline.
The new source is that pattern pointed at the chat channel.

## Goals

- A `prompts` entry may be an object `{goal, max_exchanges}` that expands into repeated client messages until the client is satisfied or the budget is exhausted.
- String entries keep today's semantics exactly (literal message, or the `DECIDE_FROM_PERSONA` sentinel).
- The three loop invariants above hold unchanged.
- Every entry's outcome (`completed`, `satisfied`, `budget_exhausted`, `fallback`) and exchange count are recorded and available to gates.

## Non-goals

- Client tool use or any environment access for the client; the client judges satisfaction from the conversation alone, like a real non-technical client.
- Environment-evidence-informed satisfaction; out-of-band verification (evidence phase, outcome judge) remains the sole ground truth.
- A richer entry schema (no stop-condition DSL, no per-entry personas); the persona stays per-case.
- A closing pleasantry after satisfaction; the satisfaction reason is recorded, not sent, because a closing message costs a full agent turn for no measurement value.

## Design

### Config schema

`prompts` becomes `list[str | GoalEntry]` where `GoalEntry = {goal: str, max_exchanges: int = 3}`.
The first entry must remain a literal string (see open question 1).
`max_exchanges` is validated at generation time against a hard cap (proposal: 8); each exchange is a full agent turn in the workspace, so unbounded budgets are not permitted.
The extended schema rides the existing transport unchanged -- the fenced JSON in `instruction.md` and `tests/case.json` -- so `environment/` stays byte-identical across the dataset and no image rebuild is triggered.
`generate.py` validation extends accordingly: non-empty goal, budget bounds, first-entry rule; the instruction prose renders goal entries distinctly from literal turns.
Authors keep setting `timeout_seconds` themselves; generation warns when the summed worst-case exchange count is implausible for the configured timeout.

### TurnSource interface

`TurnSource.next_message(case, transcript) -> str` becomes `next_action(case, transcript) -> Say(text) | Done(reason, detail)`.
`LiteralTurnSource` says its string once, then `Done(completed)`; `PersonaLLMTurnSource` makes one decider call, says the result, then `Done(completed)`.
The conversation loop gains one level: outer over entries, inner until `Done` or the entry's budget is reached; each `Say` performs the existing wait-for-WAITING, send, wait-for-reply, snapshot, and sync sequence.
The loop, not the source, enforces `max_exchanges` as a hard stop; a source cannot exceed its budget by construction.
What a spent budget *means* is declared by the source, not the loop (an `exhaustion_end` property answering the whole `Done` -- reason and detail -- so an ending the budget pre-empts is recorded exactly as one the source reported itself): a literal or decider entry that spent its budget of one is `completed`, while a cut-off goal entry is `budget_exhausted`, or `fallback` when its last allowed exchange was the degraded fallback line, since that is a harness outage rather than an agent that failed its client.
The alternative -- asking the source for one more action just to learn whether it would have said `satisfied` -- costs a discarded LLM call per exhausted goal entry, so the source declares the meaning instead.
Sources carry per-entry state (whether they have spoken, which goal they hold), so each entry gets its own source instance; usage accumulates in the driver rather than in a shared source.

### GoalTurnSource

One forced-tool Anthropic call per exchange, on the same client/model plumbing as the decider (`-m/--model`, `ANTHROPIC_API_KEY`).
The prompt carries the persona, the entry's goal, and the rendered conversation so far (the same clean rendering `render_client_conversation` produces).
The tool schema is a union: `send_message{text}` or `satisfied{reason}`; deciding "am I satisfied?" and "what do I say next?" is a single judgment, so it is one call, not two.
On any API failure or empty output, the source sends the existing fallback literal (`"Sounds good."`) once and returns `Done(fallback)`, mirroring the decider's degradation; the trial completes rather than wedging.

### Recording and accounting

`state.json` records per entry: index, kind, `exchange_count`, outcome, and a `detail` giving the source's own reason for stopping (a satisfied client's account of what met the goal, or the harness's note that the client's model call failed).
The legacy `num_turns` key keeps meaning the configured entry count while `waits_done` counts messages actually sent; the two are no longer equal for goal-bearing cases, and any reader that assumed equality must switch to the per-entry records.
The decider audit records (now the `decider_turns` list in `trajectory.json`'s `extra.minds_evals` block, since `full_transcript.jsonl` no longer exists; see `specs/minds-evals-atif-transcripts/spec.md`) gain `entry_index`, `exchange`, `entry_kind`, and `detail` fields, and the ported `turn` key becomes nullable: a call carries a turn number only once its message has reached the workspace, so a call that only decided to stop -- or one whose message a timed-out send never delivered -- has none. The judge-transcript renderer reads only the trajectory's steps, so these records never reach the judge.
Usage stays in the existing `decider_usage` bucket -- the goal source is still the simulated client -- with per-event fields distinguishing entry kinds.

### Snapshot cadence

The final exchange of the final entry is unknowable in advance, so "snapshot after the final turn" is not implementable under expansion.
Snapshot points are therefore named (`AFTER_EXCHANGE`, `AFTER_FINAL_ENTRY`) and the configured cadence selects one: `snapshot_mode=final` snapshots once after the final entry completes, and `per-turn` snapshots after every exchange.
For goal-bearing datasets `per-turn` consequently costs one workspace snapshot per exchange, not per entry; this semantic change must be called out wherever the feature is announced.

### Gates and grading

`all_turns_completed` is re-founded on entries: every entry reached `Done`, and the entries' own `exchange_count`s sum to `waits_done`. The entry records and the message counter are written by different parts of the driver, so a trial whose two views of the conversation disagree is not a gradeable record.
A string entry must still account for exactly one of those messages; only a goal entry's count is free to vary, down to zero when the first reply already satisfied the client. Without that floor the aggregate alone would let a goal entry's extra exchange pay for a deterministic turn the run dropped.
A `budget_exhausted` entry does not gate the reward to zero; it is recorded in the per-entry records, and the exchanges it produced stay in the conversation the outcome judge grades, because an agent that cannot satisfy an unreasonable goal must not be conflated with a broken trial (whether the judge is also told the outcome label is open question 2).
`agent_engaged_substantively` keys off the messages the client actually sent instead of the entry count: it asks for `min(2, messages sent)` distinct replies, so a client satisfied after one message needs one distinct reply while a longer conversation still needs two, however few of its messages drew a non-empty reply.
The gate criteria run inside the verifier container against absolute paths with stdlib-plus-rewardkit imports only, so the entry predicates are split into pure functions that are unit-testable on the host.
The oracle (`solution/solve.sh`) treats a goal entry as one literal message stating the goal; the oracle fabricates a plausible max-reward transcript and does not simulate client persistence.

## Measurement change

**Warning:** this feature resolves the discussion [outcome_verification.md](outcome_verification.md) explicitly deferred: a persistent, goal-holding client changes the thing being measured (the conversation), so scores are not comparable across this change.
Datasets and result sets must be versioned or flagged at this cut point.
Satisfaction remains part of the stimulus, not the measurement: it is conversation-only, and the evidence phase plus outcome judge remain the ground truth for whether the goal was actually achieved.

## Interaction with multi-step tasks

The mechanism composes cleanly with harbor `[[steps]]`, and better than harbor's own simulated-user does, for two structural reasons.
First, the expansion lives entirely inside one `run()` invocation -- exactly the unit `MultiStepTrial` invokes per step -- so a multi-step adoption where the driver is the per-step agent runs each step's own `prompts` list through this machinery unchanged, with no bridge lifecycle, no `SUPPORTS_RESUME`, and no exclusion guard to remove.
Second, the Minds conversation lives in the workspace, and the workspace persists with the environment across steps; target-side conversational continuity between steps is therefore automatic, unlike harbor-native agents whose sessions reset per step.

Adopting `[[steps]]` remains a separate decision with its own costs, unchanged by this spec: per-step case-config transport (multi-step tasks have no top-level `instruction.md`, so each `steps/<name>/instruction.md` carries that step's fenced JSON), workspace preparation guarded to run only on the driver's first `run()` call (driver instance state survives across steps), workspace teardown and the evidence phase moved from a single `finally` to the final step (the step index rides the per-step config), and per-step verifier content.
Nothing in this spec forecloses or depends on that adoption.

## Testing

- Unit tests (`_test.py`) for: the two-level loop with a mock source (budget enforcement, `Done` reasons, per-entry recording), `GoalTurnSource` tool-call parsing and fallback, `generate.py` validation of the extended schema, and the re-founded gates against expanded transcripts.
- Manual verification before trusting scores: one small live dataset run (per the README run recipe) with at least one goal entry per case, inspecting `trajectory.json` and the per-entry outcomes in `state.json`.

## Open questions

1. Should a goal entry be allowed as the first entry? The source can state the opening ask from the goal with an empty transcript, unlike the sentinel; this spec keeps the literal-first rule to preserve the existing semantics row until an eval needs otherwise.
2. Should the outcome judge's prompt name `budget_exhausted` entries explicitly, or is their visibility in the transcript sufficient?
3. Defaults: `max_exchanges = 3` with a cap of 8 is a proposal; calibrate against real agent-turn latency and cost after the first live run.
4. Scoring policy for `fallback` entries: the outcome is accepted by the structural gates, so a trial whose own client-model call failed is graded with the fallback line in its transcript, and a box with no `ANTHROPIC_API_KEY` degrades every goal entry yet still scores. One flaky call and a poisoned job look the same; a run-level `fallback_count` threshold in aggregation (or a gate) would separate them.
5. A goal entry satisfied at exchange 0 sends nothing, so a multi-entry case can clear every structural gate on a single message. That is correct by this design (satisfied-at-zero is the best outcome, and the outcome judge is the ground truth) but weaker than what string prompts guarantee; a floor such as "a goal entry must speak once before declaring satisfaction" is an eval-design decision, not a defect.
6. The goal client's model call is not bounded by the trial deadline, so a trial past its budget can spend one more call (up to the call timeout) and then time out with the reason "agent never reached WAITING" rather than "the client's call overran". Bounded to one call per trial; a proper bound would put a deadline into `next_action`.
7. Calibration after the first live goal run: `TYPICAL_EXCHANGE_SECONDS` (the implausible-budget warning rarely fires under the default timeout because the budget also covers workspace bring-up), and the client prompt's rule to declare satisfaction "as soon as the agent says the goal is met", which may short-circuit the push-back the goal text asks for.
8. Reviewability of goal-client transcripts: the client re-sends the whole conversation each exchange with no prompt-cache breakpoint; size the cost after real exchange counts are known.

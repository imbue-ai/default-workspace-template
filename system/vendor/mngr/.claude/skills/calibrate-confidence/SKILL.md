---
name: calibrate-confidence
description: Decide whether a diagnosis is firm enough to act on, or name the exact observability that would make it so. Produces one of two artifacts -- an actionable diagnosis with numbered fix options, or a logs proposal that converts the failure's next occurrence into a solved case. The defined output for an investigation that did not reach certainty. Used by investigate-bug locally and enforced as a schema gate by the mngr-seer investigator.
---

# Calibrating a diagnosis

You have a mechanism and some surviving hypotheses.
This step decides what kind of artifact you are entitled to produce.

There are exactly two.
Which one you may write is determined by your confidence, honestly judged -- and the judgment comes before the writing, not after.

## The gate

**High confidence** means all of the following:

- You can name the defect precisely: the file, the lines, and the conditions that trigger it.
- No competing hypothesis survives that would lead to a different fix.
- You would bet that a fix written from your description alone -- by someone who never saw the evidence -- lands correctly.

If all three hold, produce an **actionable diagnosis**.

**Anything less** -- one surviving alternative, an unread span of history, a gap you could not establish was truly empty -- produces a **logs proposal**.

A logs proposal is not a weaker fix proposal.
It is a different artifact with a different job, and writing a hedged fix proposal instead is the specific mistake this skill exists to prevent.

## Why the gate is worth obeying

A wrong-but-confident diagnosis costs more than no diagnosis.
Someone implements it, someone reviews it, and both are working from a bad premise -- and the original failure is still there, now behind a merged change that appears to have addressed it.

A good logs proposal costs one deploy and converts the failure's *next* occurrence into a solved case.
The error re-fires carrying exactly the fields that separate the hypotheses, and the investigation that was stuck becomes routine.

This is why the honest answer at medium confidence is worth more than the optimistic one.
The system gets smarter about a bug it could not crack; it does not get a fix it cannot trust.

## Artifact 1: the actionable diagnosis

- **What breaks**: the error class, the sanitized message shape, affected surfaces, frequency and user counts, first and last seen, the releases involved.
- **Diagnosis**: the mechanism as a narrative, with `file:line` references and the specific evidence -- frames, breadcrumbs, divergence between instances -- that supports it.
- **Fix options**: one to three concrete options, **numbered** (`### Option 1: ...`), so a reviewer can say "go with option 2" and be understood exactly.
  For each: what changes (files, approach), why it works, the tradeoffs, and the blast radius.
  Order them by your recommendation and say which you would pick and why.
- **Regression notes**: what a reviewer should double-check, and the riskiest assumption in the diagnosis.

Numbering is not cosmetic.
It is the interface by which a human's choice is communicated back without ambiguity, and it is what lets a fix be delegated rather than negotiated.

## Artifact 2: the logs proposal

- **What breaks**: the same as above.
- **Why the root cause is not pinned down**: each surviving hypothesis, the evidence for and against it, and what is missing to decide between them.
- **Logs to add**: the specific log lines that would disambiguate.
  For each:
  - the file and the call site;
  - **what to record -- fields, not values.**
    Name the shape of the datum (`the resolved config path`, `whether the session token was present`), never a concrete value.
    This keeps the proposal implementable without the evidence in hand, and keeps it shareable.
  - which hypothesis its output confirms, and which it kills.

Write it so that someone can implement it directly from the description, and so that a future investigator reading the resulting logs knows what question they were added to answer.

A logs proposal that says "add more logging around session restore" has done nothing.
The value is entirely in the specificity.

## Judging honestly

The pressure runs one way: toward claiming high confidence, because the actionable artifact feels like the real deliverable.
Resist it.

Useful checks:

- Could you write the fix's test -- the assertion that fails before and passes after -- from what you know?
  If not, you do not have the mechanism.
- Is there a hypothesis you dismissed because it was inconvenient rather than because evidence killed it?
- Are you relying on an absence that `correlate-evidence` did not clear?
  An unproven zero is not a dead hypothesis.
- Would you still call it high confidence if you had to name the reviewer who will be misled?

Medium and low both produce a logs proposal.
The distinction between them is worth recording for the reader, but it does not change the artifact.

## The automated consumer's enforcement

In the mngr-seer pipeline this gate is machine-enforced, and the vocabulary maps directly:

| Here | Seer's sidecar |
|---|---|
| actionable diagnosis | `kind: "fix-options"`, which **requires** `confidence: "high"` |
| logs proposal | `kind: "needs-logs"`, at `confidence: "medium"` or `"low"` |

The orchestrator validates the coupling and rejects a sidecar that claims fix options at anything below high confidence.
A logs proposal there also resolves and *unassigns* its Sentry issue, so the post-deploy regression -- now carrying the new logs -- re-enters the sweep pool automatically.
That is the feedback loop this artifact was designed to feed.

Locally there is no schema, and the gate is yours to keep.
Keeping it is what makes a stalled local investigation produce something durable instead of trailing off in conversation.

## Related skills

- `diagnose-mechanism` -- produces the mechanism and hypotheses this step judges.
- `correlate-evidence` -- clears (or fails to clear) the absences your confidence rests on.
- `investigate-bug` -- the local composition; this is its final step.

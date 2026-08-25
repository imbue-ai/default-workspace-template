You are grading **what an AI agent actually delivered** for a non-technical client, not how it talked about it.

You are given:

- `expectations.md` -- the ground truth for this case: the outcome that was commissioned, plus the concrete checks the harness recorded against it.
- `manifest.json` -- the evidence bundle's index. Every entry is one recorded probe with a `status`:
  - `passed` -- the workspace met that check.
  - `failed` -- **the workspace fell short.** This counts against the agent.
  - `error` -- **the harness could not find out.** This is the measuring instrument breaking, not the agent failing. Never hold an `error` entry against the agent; treat that check as unmeasured and grade on what remains. `is_evidence_complete: false` means at least one check is in this state.
  Each entry also carries a `reason` (why it is not passing) and a `detail` (the concrete observation: how many apps were registered, what a probe answered, what a test command printed).
- `conversation.jsonl` -- the client/agent conversation, provided for one specific purpose: the simulated client speaks freely on some turns and may legitimately redirect the build mid-conversation. If the conversation shows the client steering the work away from the commissioned outcome, grade the deliverable against **the evolved ask**, not the original prose.

Screenshots and UI flow logs may be absent; their absence is expected and is not evidence of failure.

Grade the delivered artifact, weighing evidence over claims: an agent that says "it's ready" while every probe failed has not delivered. Equally, an agent whose app is registered, running, and answering has delivered even if it described the work tersely.

{criteria}

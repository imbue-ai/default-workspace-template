# Step-based tasks

## Purpose and scope

This spec makes a minds_evals persona case executable as a harbor multi-step task: a sequence of named steps, each with its own conversation turns, its own files introduced into the workspace, and its own verification, where leaving a step is gated on that step's verification result.
Every step is verified by the *existing* task-level verification machinery (the structural gates, the quality judge, and the outcome judge with its evidence collection), parameterized per step; no new judging mechanism is introduced.
It builds directly on [goal_driven_turns.md](goal_driven_turns.md), whose turn loop runs unchanged inside each step, and on the multi-step runner facts recorded in that spec's "Interaction with multi-step tasks" section.
The audience is the engineer implementing it and the eval authors who will write stepped cases.

Out of scope: any harbor change; per-step personas; resetting application state between steps; a dedicated step-gate judge (see "Why no separate gate judge").

## Background

A harbor multi-step task calls the agent's `run()` once per step against one persistent environment, runs a verifier after every step, and aborts the remaining steps when a step's reward falls below its `min_reward`.
The minds_evals driver is that agent: it prepares the Minds workspace, drives the client conversation, collects verification evidence while the workspace is alive, and tears the workspace down.
A flat case's verifier composes three rewardkit dimensions -- `gates` (structural, 0/1), `quality` (conversation judge), and `outcome` (present only when the case declares `expectations`) -- into `reward`: the earned score, zeroed unless every gate passed.
`expectations` allows a case with an `outcome` and no `deliverable`; such a case commissions nothing probeable unless it also declares `ui_flows`, so with none its outcome is judged from the conversation alone, and rewardkit still produces the `outcome` dimension from the judge with no programmatic criteria beside it.

The motivating shape is a case whose client commissions an application from a dataset they have uploaded, later adjusts the requirements, and finally uploads an updated dataset the application must absorb.
The phases want separate turns, separate verification ("was a mockup presented and approved" versus "does the running app reflect the new data"), a hard stop when an early phase fails, and -- the property that cannot be faked -- the updated dataset must not exist in the workspace before its phase.
Shipping every dataset in the workspace template from the start and pointing at each one by an opaque directory name only hides the future from an agent that does not look; an agent that lists the uploads directory sees it.
Minds stores user uploads under `data/uploads/<id>/` in the workspace, untracked by git (the template ignores `data/uploads/*`), which is the convention step files follow.

## Goals

- A persona case may declare `steps` instead of a flat `prompts` list; each step has a name, its own prompt entries (goal entries included), and optionally its own `files`, `expectations`, and `min_reward`.
- A step's `files` are placed into the workspace when that step starts and do not exist in the workspace before it; the agent's access to them is a fact of the filesystem, not a matter of what it happens to look for.
- Each step is verified by the standard verifier with that step's expectations; a step without a deliverable is verified from the conversation and whatever UI flows it declares.
- A step's `min_reward` gates leaving the step; a failed gate aborts the remaining steps, and the trial is scored as failed.
- Verification evidence is collected at the end of every step; the workspace is torn down after the final step, or on a step the driver itself gave up on, since harbor stops calling it either way.
  A step that merely scored below its threshold is not one of those -- harbor decides that after `run()` has returned, so the driver never sees it and the workspace sandboxes are reclaimed by their own 3h lifetime instead, idling until then.
- Every step's reward is on the same scale as a flat case's, and both harbor aggregation strategies (`final`, `mean`) are legitimate. Two rewards are comparable when they were composed the same way: a step whose expectations commission nothing probeable is judge-only on the outcome dimension, which is deliberately a different composition from a deliverable step's even split with the checks, and the two must not be read against each other.
- A step that fails leaves enough on disk to diagnose it without the box: the driver log, the box-side service logs, the timeout reason, and a diagnostics capture at the moment of failure.
- Single-step cases are unchanged.

## Non-goals

- A bespoke LLM step-gate judge; the author's exit criteria are expressed as the step's `expectations.outcome`, graded by the existing outcome judge.
- Application-state reset between steps; the convention in "Flow side effects" applies instead.
- A per-step oracle beyond replaying each step's prompts as literal messages.
- Continuing past a failed gate; harbor's abort is the intended semantics.

## Design

### Config schema

A stepped case replaces `prompts` with `steps`.
The worked example is the three-phase roadmap case: a dataset the client has already uploaded, a requirement adjustment, and an updated dataset that appears only in the last step.

```json
{
  "id": "project-roadmap",
  "persona": "Head of product at a small startup. Non-technical, but knows their own projects well.",
  "steps": [
    {
      "name": "build-from-data",
      "files": [{"source": "datasets/roadmap-v1", "upload_id": "41e940fcd33540078ab77fd79f3b3943"}],
      "prompts": [
        "Can you build me an editable roadmap tool? I downloaded a pull of my delivery milestones, it's in /home/user/workspace/data/uploads/41e940fcd33540078ab77fd79f3b3943. Sketch me something first so I can see where you're heading.",
        {"goal": "See a concrete mockup of the roadmap view built around your real milestones and sign off on it, pushing back until the layout is clear.", "max_exchanges": 4}
      ],
      "expectations": {
        "outcome": "The agent presented a concrete mockup or design of a roadmap drawn from the uploaded export and the client approved it; nothing needs to be built or running yet."
      },
      "min_reward": {"gates": 1.0, "outcome": 0.5}
    },
    {
      "name": "adjust-requirements",
      "prompts": [
        "The sketch is right, go ahead and build it for real now, with my edits saved. And one more thing: about half the milestones have a second owning team, and the exec team will want to filter by team. Can you fold that in?",
        {"goal": "Get the real, saving version built, not the sketch: keep going until you can open the running roadmap, filter it by team, see two-team milestones handled, and rename a milestone and have the rename still be there after a reload.", "max_exchanges": 3}
      ],
      "expectations": {
        "outcome": "A working roadmap view as a running Minds app, populated from the uploaded export, with a filter by team and edits that are saved rather than a sketch that keeps nothing.",
        "deliverable": {"kind": "minds-app"},
        "ui_flows": [{"name": "filter-by-team", "steps": "Open the roadmap. Filter to a single team.", "expect": "Only that team's milestones remain visible."}]
      },
      "min_reward": {"gates": 1.0, "outcome": 0.5}
    },
    {
      "name": "updated-dataset",
      "files": [{"source": "datasets/roadmap-v2", "upload_id": "985e2d4f7eb948b3b45a8f0923521ab8"}],
      "prompts": [
        "I downloaded an updated pull of the data, it's in /home/user/workspace/data/uploads/985e2d4f7eb948b3b45a8f0923521ab8. Can you bring the roadmap up to date with it?",
        {"goal": "Confirm the roadmap now reflects the updated data, that the team filter still works, and that renaming a milestone still sticks after a reload.", "max_exchanges": 3}
      ],
      "expectations": {
        "outcome": "The running roadmap reflects the updated export (new and changed milestones present, removed ones gone), keeps the team filter, and edits survive a reload.",
        "deliverable": {"kind": "minds-app"},
        "ui_flows": [
          {"name": "updated-content", "steps": "Open the roadmap. Check once whether a milestone only the updated export carries is listed. Then check once whether a milestone the updated export dropped is listed. One look each is enough.", "expect": "The added milestone is listed and the dropped one is not."},
          {"name": "edit-persistence", "steps": "Edit a milestone's title to 'Renamed by eval'. Reload the page.", "expect": "'Renamed by eval' is still visible after the reload."}
        ]
      }
    }
  ],
  "reward_strategy": "final"
}
```

- `name` must match `^[a-z0-9][a-z0-9-]*$`, and must be unique within the case. It is narrower than a case id, which is unvalidated: a step name has to serve as a task subdirectory, a harbor step name and a verifier container session at once.
- `files` lists what the client "uploaded" for this step: `source` is a file or directory relative to the eval config file, and `upload_id` names the directory it appears under in the workspace's `data/uploads/`, so the prompt can refer to the same path the client would see in Minds.
  Each `upload_id` must be unique across the case, and a step's files do not exist in the workspace before that step.
- `prompts` has the schema of [goal_driven_turns.md](goal_driven_turns.md); the case's first entry must be a literal, and a later step may open with a goal entry.
- `expectations` per step has exactly the case-level schema; a step that omits it is graded on gates and quality only, like a flat case without expectations.
- `min_reward` is a scalar (gates on `reward`) or a mapping over the reward dimensions `gates`, `quality`, `outcome`, `reward`, in harbor's own `min_reward` semantics (a missing key counts as `-inf`).
  It is rejected on the final step, where harbor would judge it and then ignore it.
- `reward_strategy` selects harbor's `multi_step_reward_strategy`; the default is `final`.
- A case-level `expectations` on a stepped case is rejected: every step states its own, so that a reader of a step's instruction sees what that step is graded on.

Generation warns when a non-final step declares no `min_reward` (see "Ungated steps") and when a non-final step's `ui_flows` are declared (see "Flow side effects").

### Generated task layout

A stepped case generates a harbor multi-step task:

```
task.toml              [[steps]] with name, min_reward, per-step agent and verifier timeouts;
                       multi_step_reward_strategy
environment/           byte-identical across the dataset, as today
steps/<name>/
  instruction.md       the step's prose plus the fenced JSON config for THIS step
  solution/solve.sh    the oracle for this step: the conversation up to and including it,
                       replayed as literal messages
  workdir/             present only for a step with files
    step_files/<upload_id>/...   this step's files, copied from the eval config's sources
    setup.sh           moves step_files/ out of the box's working directory to /work/step_files/<name>/
  tests/               a complete copy of the standard verifier build context whose case.json
                       carries this step's expanded expectations
```

There is no top-level `instruction.md`, `tests/`, or `solution/`; harbor requires none of them when every step ships its own, and it prefers a step's `solution/` over a task-level one.
The oracle is per step because the structural gates hold a step answerable for the conversation so far: one task-level script replaying the whole case into every step would fail every earlier step's `all_turns_completed`.
Harbor validates nothing about step names and silently ignores unknown `[[steps]]` keys, so generation's own rules are the only protection.
In `separate` verifier mode a step's `tests/` *replaces* the task's build context, so each step ships the whole verifier; the Dockerfile copies `case.json` last so the per-step images share every other layer.

The per-step fenced config is the flat `CaseConfig` plus a `step` block (`name`, `index`, `total`, `trial_lifetime_seconds`, `entries_before` -- how many prompt entries earlier steps hold, so the gates can count the cumulative conversation -- and the step's `files` as `{upload_id, box_path}` pairs), with `prompts` and `expectations` being the step's own.
The verifier's `case.json` is the same object, so the collector and the judge of a step cannot disagree about what that step expects.

### Driver

`run()` is invoked once per step against the same driver instance and the same workspace.

- Workspace preparation runs on the first call only; every later step reuses the workspace, and the conversation inside it continues where the previous step left off.
- The step's files are placed into the running workspace after preparation and before the step's first message (see "Step files"); a step whose files cannot be placed is marked timed out with that reason, since a conversation about an upload that is not there measures nothing.
- Each step's `prompts` go through the goal-driven turn loop unchanged.
- **The evidence phase runs at the end of every step**, against that step's expectations and within that step's verification budget; the workspace is torn down afterwards by the final step, or by a step the driver gave up on (a step that only scored below its threshold leaves it to the sandbox lifetime, as above).
  A step whose expectations commission no deliverable collects the conversation-side evidence plus any UI flows and `test_commands` it declares (no HTTP or file probes, no bundle): only the bundle follows `deliverable`, while flows and test commands are declared independently of it.
- Per-entry records, `waits_done`, and the configured entry count accumulate across steps, so the final step's `state.json` reconciles with the whole case and the structural gates read the conversation so far.
- Harbor empties the box's `/logs/agent` before each step and archives each step's agent directory, so each step's evidence bundle is step-local while `trajectory.json` and `state.json` are cumulative prefixes; the evidence directory is re-created on every call.
- The trajectory is published at the end of every step, by the same fallback chain a flat case uses (`specs/minds-evals-atif-transcripts/spec.md`), and every step's document covers the whole conversation so far: the steps share one workspace, so the document its agent builds is naturally cumulative, and the hand-built shape is built from the driver's own accumulating conversation. That is the scale the gates read it on, since they hold a step answerable for every entry configured so far.
- The transcript capture and the worker capture are part of that per-step evidence phase, so a worker still alive when a later step collects is captured again by it; each step's bundle and trajectory therefore stand on their own rather than pointing back at an earlier step's.
- Harbor sums one `AgentContext` per step into the trial's totals while the usage account the driver resolves is the whole conversation's, so each step publishes only the spend since the previous step's publish. An output-token or cost figure the stream never reported publishes nothing rather than a partial one, and leaves what earlier steps published standing.
- Diagnostics per step: the driver writes its own log file for the duration of each call, the box-side service logs (backend, tunnel, proxy) live outside `/logs/agent` in a location harbor collects and never resets, workspace snapshots stay under `/logs/agent` where each tarball travels exactly once instead of being re-collected on every later step, `state.json` carries `timed_out_reason`, and marking a timeout captures a bounded diagnostics file (workspace agents listing, chat agent state, service-log tails).
- Workspace readiness has its own budget, and preparation gives up at whichever of that and the conversation deadline comes first, so a workspace that never answers fails with a named reason instead of consuming the step's whole budget.

### Step files

Files travel in two hops, because neither party can reach the other end directly.
Harbor uploads `steps/<name>/workdir/` into the box's working directory -- the environment's `workdir`, which for this task is the staged mngr checkout at `/work/mngr` -- before that step's agent runs, and `setup.sh` (run by harbor as the step's agent user) relocates it to `/work/step_files/<name>/` and deletes itself; that is the only channel from the task directory into the box, since the driver never sees the task directory and the environment image must stay byte-identical across the dataset (a dataset that differs per step or per case would rebuild the image).
A non-zero `setup.sh` aborts the step and every remaining step, which is the right outcome for a step whose files did not arrive.
The driver then copies each `step_files/<upload_id>/` into the running workspace at `/home/user/workspace/data/uploads/<upload_id>/` with `mngr rsync` in the box-to-workspace direction, before the step's first message; the destination sits inside the workspace's git worktree, so the transfer runs with `--uncommitted-changes clobber`, because the default mode refuses or stashes over the agent's own in-progress work.
The files land untracked, like a real Minds upload (the template ignores `data/uploads/*`), so they never enter the eval-case commit or the deliverable bundle; verification that the agent used them goes through the outcome judge and UI flows, not through file inventory.
Absence before the step is a consequence of the mechanism, not a policy: the file is not in the template, not in the image, and not in the workspace until the driver places it.
Large datasets are the author's responsibility to keep reasonable; the sources are copied into every task directory that uses them and travel once per step.

### Verification per step

Each step runs the standard verifier over the archived step artifacts: the gates over the cumulative conversation and entry records, the quality judge over the conversation, and -- when the step declares expectations -- the outcome judge over the step's evidence.
The reward composition is unchanged (`reward` = earned score, zeroed unless every gate passed), so a step's `reward.json` has the same keys and scale as a flat case's, and harbor's `min_reward` gates on those keys directly.
Grading-infrastructure failures keep their existing semantics: no parseable reward file, so harbor errors the trial rather than grading a fake zero. The remaining steps stop either way -- harbor aborts on a step that has an exception and no verifier result -- but the trial is recorded as an error rather than as a scored failure, which is what keeps it out of the results instead of in them as a zero.

### Why no separate gate judge

An author's exit criterion for a step ("a mockup was presented and approved") is exactly what the outcome judge grades when the step's `expectations` carry that prose and no deliverable.
Reusing it keeps one judging path, one prompt, one evidence contract, and one reward scale, and it means the gate's own reasoning appears in the same `reward-details.json` an analyst already reads.
The trade-off is crispness: outcome scores are graded, not binary, so a threshold is a judgment call the author makes per step; the recommended shape is a mapping that gates on `gates = 1.0` plus an outcome threshold, with the rewardkit reasoning available to calibrate it.

### Timeouts

The case's `timeout_seconds` is split across steps in proportion to each step's worst-case exchange count and emitted as per-step `[steps.agent] timeout_sec`; harbor otherwise gives every step the full task-level budget, silently multiplying an N-step case by N.
Every step receives the verification budget, since every step collects evidence, and every step restates `[steps.verifier] timeout_sec` so that the figure a reader of a step sees is the one that step gets, rather than one inherited from the task-level `[verifier]` block that also configures the task verifier a stepped task never runs.
Resources that must outlive a step (the proxy tunnel) are sized from the trial's whole lifetime, not the step share and not the conversation total: between two conversations the trial also spends a step's evidence phase, its cleanup grace and its verifier container.

That lifetime runs into two ceilings no case config can raise, and a stepped case has to fit inside both.
The one workspace its steps share is created on the `modal_eval` overlay, whose sandbox lifetime is 3h; generation warns when a case's worst case exceeds it, and the only remedies are a shorter `timeout_seconds`, a shorter `verification_timeout_seconds`, or fewer steps.
The box is capped separately by the run recipe's `--ek sandbox_timeout_secs`, and has to survive every step's agent run plus the verifier of every step but the last, so a long stepped dataset passes its own larger value through.
A flat case is nowhere near either.

### Reward aggregation and abort

`final` (the default) scores the trial by the final step's reward; `mean` averages the steps that produced a reward.
Both are legitimate because every step's reward is on the flat-case scale, with one caveat for `mean`: harbor leaves aborted steps out of the denominator, so an early abort raises the mean rather than contributing zeros.
`harbor trial regrade` refuses multi-step tasks, so re-scoring a stepped trial means re-running it.
A gate-aborted trial is scored, under `final`, by the aborted step's full graded reward -- a real measurement of the step the agent failed, not a bare 0/1 -- and the abort is visible in the trial's step results.

### Ungated steps

A non-final step without `min_reward` never aborts, so after an earlier failure harbor would run the next step against a workspace that will not answer.
The driver fails fast in that situation (a later `run()` finds the trial already timed out and returns), and generation warns; authors should gate every non-final step.

### Writing goals and flows

A goal entry measures the agent only as hard as the client it describes pushes.
A goal that is satisfied by seeing something work is satisfied by a convincing sketch, so a step whose verifier expects a real deliverable must give the client a goal that demands it ("get the saving version built, keep going until a rename survives a reload"); otherwise the trial measures the client's passivity as much as the agent, and the evidence phase fails a deliverable the conversation approved.
A flow that checks an absence should ask for one lookup per fact and say that one look is enough; the verification agent proves a negative by searching and then tends to re-prove it until its step budget runs out, recording as a failure a state whose evidence it already holds.

### Flow side effects

UI flows are not read-only: a persistence check that renames an item leaves that rename in the application for every later step, where the next step's agent and goal client will see it.
Convention: intermediate steps declare read-only flows (open, read, filter) and reserve mutating checks for the final step; generation warns on any non-final `ui_flows` so the author confirms the flows are read-only.

### Cost

The evidence phase is the expensive part of a trial (browser flows, screenshots, judge calls, a bundle, a snapshot), and it now runs once per step; a two-step case costs roughly twice a flat one to verify.
This is a nightly-job feature, not a per-PR gate.

## Testing

- Unit: stepped config parsing and rejection rules (case-level expectations, final-step `min_reward`, name rules, duplicate `upload_id`, missing file sources); generation of the task layout including `workdir/step_files/` and `setup.sh`, loaded through harbor's own `Task` model; per-step timeout splitting; the driver placing a step's files before its first message and invoking the evidence phase on every step, with teardown on the final step and on the step the trial gave up on but on no other, against the existing mock environment; gates over cumulative state.
- Live, before trusting scores: a flat control case with the same mngr/dwt pins (a known-good pair, not two unpinned trunks), then the three-step roadmap case above, checking that every step directory carries its driver log, service logs, evidence bundle, and reward details, and that the updated dataset is absent from the step-2 snapshot and present in the step-3 one.

## Open questions

1. Should a non-final step without `min_reward` receive an implicit `gates = 1.0` rather than only a warning?
2. Should the quality judge run on conversation-only steps, or only on steps with a deliverable?
3. Is a per-step workspace snapshot still worth its cost once every step ships an evidence bundle?
4. Should a later step be able to *replace* an earlier upload in place (same `upload_id`), or is a new id per upload -- the shape a real client produces -- the only supported form?

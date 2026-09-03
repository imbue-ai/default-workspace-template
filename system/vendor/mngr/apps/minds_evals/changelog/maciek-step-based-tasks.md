A persona case can now declare `steps` instead of a flat `prompts` list, and generates a harbor
multi-step task: one workspace for the whole trial, prepared on the first step and torn down after
the last, or on a step the driver itself gives up on. A step that only scored below its threshold
is not one of those -- harbor decides that after the driver has returned -- so a gate-aborted
trial's workspace sandboxes, which outlive the box that made them, idle until their own 3h lifetime
reclaims them. The Minds conversation carries on across a step boundary, with nothing replayed or
resumed.

Each step names itself and carries its own turns (goal entries included), its own `expectations`
with exactly the case-level schema, and a `min_reward` -- the reward it must reach for the trial to
go on, in harbor's own form: a bare number gates the composed `reward`, an object gates each
dimension it names (`gates`, `quality`, `outcome`, `reward`). Below the threshold, harbor aborts
every remaining step. A step's exit criterion is therefore ordinary `expectations` prose graded by
the existing outcome judge, on the same reward scale as a flat case, with the judge's reasoning in
the same `reward-details.json` an analyst already reads.

Every step is graded by the standard verifier -- the structural gates, the quality judge, and the
outcome judge with its evidence -- so the evidence phase now runs at the end of every step, against
that step's expectations and within its own verification budget. A three-step case therefore costs
roughly three times a flat one to verify.

Each step publishes its own `trajectory.json`, covering the whole conversation so far rather than
that step alone: the steps share one workspace, so the ATIF document its agent builds is naturally
cumulative, and the hand-built fallback is built from the driver's own accumulating conversation.
The workspace-transcript capture and the launched-worker capture run in every step's evidence
phase, so a worker still alive when a later step collects is captured again by it and each step's
bundle stands on its own. A step that collects nothing -- its workspace already torn down by the
step that gave up -- reports its own `trajectory_source` and `transcript_capture` rather than the
last step that did collect. Harbor sums one agent context per step into the trial's totals, so a
step reports only the workspace spend since the previous step's publish rather than the running
total.

A step may introduce files. `steps[].files` names a `source` relative to the eval config file and
the `upload_id` it takes in the workspace's `data/uploads/`, so a prompt can quote the same path the
client would see in Minds. The files do not exist in the workspace before that step: generation
stages them in the step's harbor workdir, a generated `setup.sh` relocates them inside the box, and
the driver copies each into the running workspace before the step's first message. They land
untracked, exactly as a real upload does, and a placement that fails ends the trial with that
reason. New information can therefore be introduced as real, previously absent files rather than
hidden ones.

`configs/eval-config-stepped.json` is the worked example of a step-based persona case: a
project-roadmap client who uploads a pull of their delivery milestones and asks for a mockup, then
asks for a two-team filter once the mockup is signed off, then uploads an updated pull the roadmap
has to absorb. Each step carries its own expectations, the first two gate on them before the trial
may go on, and the updated dataset appears in the workspace only when its step starts.

The two synthetic datasets it ships live beside it in `configs/datasets/roadmap-v1/` and
`configs/datasets/roadmap-v2/`: forty delivery milestones with owning teams, dates, statuses and
dependencies, plus a team list. The v2 pull adds six milestones, drops five, changes the dates,
statuses or owners of seven, and retitles one, so "did the roadmap actually absorb the update" is
answerable by naming specific milestones.

A stepped case runs into one ceiling its own config cannot raise: the single workspace its steps
share is created on the `modal_eval` overlay, whose sandbox lifetime is 3h, and a case whose worst
case outlasts that loses the workspace mid-trial. Generation warns, naming both figures, before it
builds anything. The box has a separate cap in the run recipe (`--ek sandbox_timeout_secs`), which a
long stepped dataset raises by passing its own value through. The shipped example is sized to fit
both without a raised cap: a 4500s conversation budget and a 900s verification budget across its
three steps.

`reward_strategy` (`final` by default, or `mean`) selects how per-step rewards become the trial's.
Both are legitimate now that every step is graded on the flat-case scale; under `final`, a trial
aborted at a step is scored by that step's own graded reward.

`expectations` may state an `outcome` with no `deliverable`. Such a block expands to no HTTP or file
checks and no bundle, and the collector records only its always-on capture; with no `ui_flows`
either, the outcome dimension is the judge reading the conversation, and those scores are not
comparable with a deliverable case's even split between the judge and the checks. `ui_flows` are
independent of `deliverable`, so a step can probe what an earlier step delivered without
commissioning anything of its own.

The case's `timeout_seconds` is split across the steps in proportion to their worst-case exchange
counts, so a stepped case cannot outrun its declared budget; every step is additionally given the
evidence-collection budget it now spends, and each step restates its verifier timeout so that the
figure a reader of a step sees is the one that step gets. Anything started on the first
step that a later one still needs -- the reverse tunnel the workspace reaches the LLM proxy on -- is
sized against the trial's whole lifetime, which is the conversation budget plus the evidence phase,
cleanup grace and verifier container every step spends between two conversations.

Generation rejects a case that declares both `prompts` and `steps`, a case-level `expectations` on a
stepped case, a `min_reward` on the last step (harbor would grade it and then ignore it), a step name
that cannot name a directory, duplicate step names, a duplicate `upload_id`, and an upload whose
source is missing or outside the eval config's own directory. It warns when a non-final step declares
no `min_reward` (nothing can then abort the trial) and when a non-final step declares UI flows (a
flow is not read-only, and whatever it changes stays changed for every later step).

`harbor trial regrade` does not support multi-step tasks, so re-scoring a stepped trial means
re-running it.

The verifier's build context keeps its criteria under `verifier/` and copies them before
`case.json`, so steps declaring the same scoring dimensions share every layer beneath the case data.

A trial that gives up now says why, and leaves enough behind to act on it. `state.json` and the trial
metadata carry a `timed_out_reason` naming the wait that ran out, and a
`timeout_diagnostics.json` records what the workspace
looked like at that moment -- its agents listing, the chat agent's state, and the tails of the box's
service logs -- captured while the workspace still existed. Every key is always present: a capture
that could not be taken says which of the three reasons stopped it (the workspace was torn down by
an earlier step, no workspace ever existed, or it never got a chat agent) rather than going missing.

The driver's own log is now an artifact: `driver.log` beside the trajectory, written per step, so a
step that wedged before it could send anything still leaves a timestamped account of where it was.

The box's long-running service logs (the Minds backend's `box.log`, the reverse tunnel's, and the
proxy's) live in `/logs/artifacts/minds/`, which harbor collects after every step and never empties.
They have to: harbor empties the agent logs dir before every step, which would unlink a log its
writer still holds open and leave the service appending to a dead inode for the rest of the trial.
Workspace snapshots stay under the agent logs dir, where each one travels exactly once.

Every readiness poll now says, once a minute, that it is still waiting and what the workspace is
answering while it waits -- the agents listing it sees, or the chat agent's state -- so a wait that
never finishes says why rather than falling silent.

Workspace preparation -- create, sign-in, creating the chat -- runs against its own 1200s
budget instead of the case's whole conversation timeout, so a workspace that comes up dead is
reported within twenty minutes rather than after consuming the entire case. The conversation
deadline still caps preparation and still governs the turns.

The host-side `trajectory.json` and `state.json` are kept current while the driver is still waiting
for a reply, not only once the reply lands: the messages of the turn in flight are carried as the
trajectory's last agent step. So a turn cut short by a dying box or workspace leaves everything the
agent said up to that point in the step's archive.

A task's `instruction.md` (and each step's) now reads as what it is: a description of the trial for
the people who run the eval and read its results. It says that the client is played by the
deterministic driver, that a model is consulted only inside the entries that call for one, and that
the fenced JSON block is the only part the driver reads.

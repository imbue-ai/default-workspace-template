Grading now reads only the trial's ATIF trajectory, and that trajectory comes from mngr's common
transcript whenever the workspace can provide it.

- `trajectory.json` is the one conversation artifact the verifier sees, as it is for any other harbor
  eval. The judge-transcript renderer, the structural gates, and the wordiness guard take the
  conversation from its ATIF steps; the outcome judge reads the rendered `judge_transcript.txt`.
  `full_transcript.jsonl` and `conversation.jsonl` are no longer written, and the verifier has no
  knowledge of the workspace UI feed or of pre-ATIF record shapes.

- The driver keeps `trajectory.json` in the box after every turn as its hand-built turn summary, so a
  timed-out trial still leaves a gradeable record, and replaces it with the workspace's own document
  once the evidence phase has captured one.

- The evidence-collection phase gains an always-on capture step: it runs `mngr transcript
  <chat-agent> --format jsonl` and `--format atif` inside the live workspace, pulls both files into
  the bundle (`verification/common_transcript.jsonl`, `verification/workspace_trajectory.json`), and
  downloads them host-side. Each half is recorded on its own, with a reason and a stderr tail when
  it could not be captured. The capture adds no manifest entry, so a transcript problem never reads
  to the outcome judge as an unmeasured check.

- The published document is the workspace's ATIF trajectory (steps, tool calls, observations,
  thinking, embedded proxy-subagent trajectories) with `final_metrics` replaced by the trial's
  resolved usage (the figures harbor's `agent_result` carries) and a root `extra.minds_evals` block
  recording the driver, the decider model and its turns, the harbor session, the case, and the usage
  source. The hand-built shape remains the fallback and carries the same block with
  `source: "hand_built"`; the decider model no longer masquerades as `agent.model_name`.

- Trial metadata gains `trajectory_source` (`workspace`, `hand_built`, or `none`) and
  `transcript_capture` (per half: whether it was captured, else the reason and detail).

- A workspace built from a pre-ATIF mngr, a failed capture, or a failed final upload all grade on
  the hand-built document. Oracle runs write a hand-built trajectory of the canned conversation.

- Background workers the agent launched through the launch-task skill are captured too: discovered
  from the launch commands in the agent's own captured stream, each worker's ATIF document, stream,
  and lead-side report are brought out (`verification/workers/<name>/`; a worker destroyed after
  finishing is read from mngr's preserved copy of its stream), and the workers are embedded in
  `trajectory.json` under the launching call as ATIF `subagent_trajectories`. Their tokens are summed
  into the trial's transcript usage account, so a trial whose only delegation was to workers captured
  after they settled is `is_cost_complete: true` (a worker still running at capture time, or one whose
  state could not be established, is summed but leaves the account incomplete); `metadata.workers`
  lists each launched worker with what was captured and its own usage account.

The driver's turn detection, usage accounting, and word-count metadata still read the feed it polls
during the conversation. Design notes: `specs/minds-evals-atif-transcripts/spec.md`.

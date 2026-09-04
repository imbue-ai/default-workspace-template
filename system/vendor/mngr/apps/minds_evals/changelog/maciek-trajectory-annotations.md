The driver's own view of a trial is preserved again, and a stepped task's trajectory says where each
step begins.

- `agent/driver_events.jsonl` holds what the harness saw: the workspace UI feed the driver polled,
  followed by one record per decider-model call with the message it produced. The trajectory's
  `extra.minds_evals.decider_turns` records the same calls without their text, so this is where a
  conversation that went wrong on the harness's side of the wire can be read back -- replies the
  driver could not make out, or an eval that has drifted from the workspace template it drives. It
  is an operational artifact beside `mngr_forward.jsonl` and `driver.log`: written host-side only,
  never mirrored into the box, and read by nothing in the grading path.

- A multi-step task's trajectory marks each step's first turn with a `system` step naming it
  (`Step: <name>`, tagged `extra.minds_evals.kind: "step_boundary"`) under a `MINDS EVALS` banner
  rule that sets it apart from the workspace's own `system` steps. Every step replays the
  conversation from its first turn, so without the marker the later steps read as one undivided
  conversation. The marker is cosmetic and reaches no judge: `system` is the source the
  judge-transcript renderer, the structural gates, and the wordiness guard all already skip, and
  `final_metrics.total_steps` stays the conversation's own count. In the workspace's own document
  the marker is placed at the step's opening client message, or by timestamp when that message is
  not in the document; a boundary that resolves to neither is dropped rather than placed on a guess.
  A task without steps gets no marker.

- `agent/instruction.md` keeps the instruction a run was handed beside the results it drove, so the
  case (or, for a multi-step task, the step) can be read in `harbor view` next to its trajectory
  rather than from the generated dataset, which lives outside the job. It is written before the
  instruction is parsed, so one that cannot be parsed is still on disk, and it stays host-side: the
  expectations it carries are never mirrored into the box.

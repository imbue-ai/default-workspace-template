- The viewer renders delegated agents. A worker the agent launches, or any harness it hands work to,
  writes its own trajectory into the parent's `subagent_trajectories`; until now the parent's step
  showed only the tool call that started one, so the whole of a delegated agent's work was absent
  from the trajectory it belongs to.

- Two ways to fold one in, chosen from a toggle in the Trajectory header, since which one reads best
  depends on what you came to find. **Nested** collapses a delegated agent under the step that
  spawned it. **Flat** merges every agent's steps into one timeline in the order they happened,
  headed wherever the timeline changes hands -- which is how you see that a worker was still running
  while the agent that launched it carried on.

- An agent yields the timeline and takes it back repeatedly, so Flat tells a beginning from a
  resumption: a delegated agent's first header carries its kind and the size of its whole
  trajectory, and the rest carry a resume marker and how much further this stretch runs. Each header
  reads from the outside in -- the agents it is nested under, then the name of whoever holds the
  timeline, then what is known about the run -- with only that name in full strength, since it is
  what you are scanning for.

- A worker still running when the trial collected it says so, in both ways of reading, since a
  transcript that stops mid-flight is otherwise indistinguishable from one that finished quietly.

- Delegation that runs deeper than one level reads either way: a worker that delegates in turn nests
  again in Nested, and takes its own place on the timeline in Flat.

- A delegated agent's steps sit behind a yellow band, one shade darker for each level of delegation,
  and are drawn exactly like the parent's, so its own sources, timings and token counts read the same
  way. Its step numbering is its own, which is what the format says it should be. In Flat a step
  carries one band per level it is nested under, so how deep a run sits reads off the left margin
  rather than off the names in its header.

- Time only decides which agent holds the timeline; within one agent, its own order stands. A
  delegated agent's step never sorts above the step that launched it -- a worker runs in a box of
  its own, so its clock is not its launcher's, and a few seconds of skew would otherwise open the
  worker's transcript above the tool call that started it. And a step never sorts above the step
  before it in its own trajectory, which is what keeps a `Step: <name>` marker with the step it
  heads: the marker is stamped on the host and the turns around it in the box.

- A reference whose trajectory was not embedded is skipped rather than stubbed, since there is
  nothing to show for it. The opposite case is shown rather than dropped: a delegated agent whose
  launching step is absent from the document is still embedded by the producer, and now appears --
  at the end of the step list in Nested, and in its own place in time in Flat.

- A delegated agent named by more than one step, such as a launch and a later await, is drawn once,
  under the step that started it.

- A delegated agent is called whatever the document calls it. A worker the harness grafts on carries
  its own name; mngr's own `Task` siblings carry only ATIF's `agent` block, which names the harness,
  so several of those in one trajectory all read `claude` and are told apart by the step each hangs
  from.

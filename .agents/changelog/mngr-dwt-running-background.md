A background agent that leaves work running is still counted as working.

mngr grew a state for an agent whose turn has ended while work it kicked off is still running. Two places listed the states they counted as live, so an agent in the new state read as finished:

- When a lead hands work to a background agent and waits for its report, a background agent that ended its turn with something still running counted as gone, so the lead stopped waiting and looked for a report that was not there yet. It now keeps waiting.

- Before the workspace updates itself it pauses the chats that are doing something. One carrying work past its turn now counts as doing something, so it is paused along with the rest.

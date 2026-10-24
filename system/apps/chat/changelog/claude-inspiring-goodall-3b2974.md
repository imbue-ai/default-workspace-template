While a chat moves to another harness, the model chip names where it is going (the picked model, or the target's name when none was picked) on every page watching it, instead of showing the model the chat is leaving and then the new agent's settings one write at a time.

Once a handoff's switch has landed on the transcript, the page no longer also shows a second "Handing off to…" spinner below it while the chat finishes delivering to the new agent.

Switching a chat to another provider is quicker: the agent the chat leaves now stops while the new one is created, instead of first. On an idle workspace an empty chat's switch between two Pi accounts went from about 14.4 s to 10.8 s; the saving is however long the old agent takes to stop.

Once a switch has landed on the new agent, the chat reads as that agent while the switch finishes handing it the messages it owes: the status line above the composer shows the agent's own activity instead of "Starting …", the chat list shows its real status, the composer has its usual placeholder, and Stop is offered for its turn. A Stop pressed then waits on the server for those messages to be delivered before it interrupts, so none is lost to the restart.

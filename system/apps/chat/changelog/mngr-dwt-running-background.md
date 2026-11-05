A chat whose agent is working on something it started no longer reads as stopped.

mngr grew a state for an agent whose turn has ended while work it kicked off is still running -- a background command, a helper agent, a watcher -- so the next thing to move the chat is that work rather than you. The chat app decided whether an agent was alive by listing the states it recognised as alive, and a state missing from that list reads as dead: such a chat showed as stopped, its row hid the work, a plain turn ending dropped its archived members, and a timer would have restarted an agent that was busy.

Alive and "has a turn in flight" are now separate questions. A chat carrying work past its turn is alive, but no turn is open, so the live-turn reading it feeds is unchanged.

Its row also shows as working on something again for the kinds of task this workspace keeps no record of its own for -- a tool call out to another service, a teammate, a cloud session -- which mngr reports and the markers here do not. A turn in progress or a permission request still takes precedence over it.

Only chats created after this reaches the workspace report the new state: mngr writes what an agent needs to report it into the agent's own area when the agent is made, so a chat that already exists keeps running what it was made with, and a new turn does not change that. Those chats behave exactly as they did before, and the workspace's own record of background tasks is what keeps them showing as working.

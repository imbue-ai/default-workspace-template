The harness registry knows about `opencode` and `antigravity`, so an agent created on either is treated as itself rather than mistaken for Claude. Both appear in the New Tab launcher.

Registering them was not optional. An unregistered harness is not neutral: `parse_harness` falls an unknown agent type back to Claude, which would have pointed Claude's transcript watcher at another harness's state directory. But a `HarnessSpec` requires a watcher, an activity tracker, a model resolver and a model catalog, and neither harness has any of those yet -- so both name a shared set of deliberately inert placeholders instead. The placeholder transcript is empty, the placeholder catalog offers nothing, and switching a model does nothing. The one live signal is the activity dot, which follows mngr's `active` marker: both plugins already maintain it, so a turn in flight shows as "Thinking…" even with no transcript behind it.

Each harness drops its own placeholder when it lands a real implementation, one harness at a time; when the last one does, the shared placeholder module goes with it.

Both harnesses launch through the shared OOM band wrapper. A test now reads `.mngr/settings.toml` and fails if any registered harness has no wrapper in its launch command -- an unbanded harness is silent (nothing errors, the agent is just disproportionately likely to be killed under memory pressure), which is how Codex and Pi went unbanded unnoticed.

OpenCode gets no New Tab tile. The harness stays registered -- so an opencode agent created from a terminal is identified as itself rather than mistaken for Claude, and its mngr plugin stays on the same launch contract as the others -- but it is not planned to get a transcript watcher, and a tile would promise a chat that always renders blank.

An Agy chat now shows its conversation. The tab was rendering blank -- the harness was registered so it could launch, but nothing read its transcript.

Agy keeps each conversation in its own SQLite database rather than a log file, so the reader tails that store directly, and it is careful about half-written rows: it only advances past a step once that step has settled, and re-reads anything still in progress until it does.

The activity indicator and its caption come with it. Agy records a tool call the moment it dispatches one, with the tool's name and arguments already filled in, so the chat can say "Running python3 showcase.py" while the command is still running rather than after it finishes. One quirk needed handling: Agy writes a short empty "deciding what to do next" step before each tool call, which would otherwise look like a finished answer and make the indicator flicker to idle between every tool.

The model bar is not part of this and still shows nothing for Agy.

Agy's tool results now go through the same handling as every other harness. Permission prompts appear as the usual approval card instead of raw text, so an Agy agent asking for access can actually be answered. Task-tracker bookkeeping commands are hidden as the structural markers they are rather than shown as work. And when a long tool result is shortened for display, the parts the chat reads out of it -- the approval request, the step-tracking lines -- are kept instead of being cut off the end.

The indicator also settles correctly after an interrupted restart. Agy picks its conversation back up where it left off, so a tool call left unfinished by the previous run was still in the store and could leave the indicator running forever; each launch now records when it started, and anything older than that is treated as finished.

Creating an Agy chat while Agy is signed out is now refused up front, with instructions, instead of producing an agent that can never take a turn -- the same preflight Codex and Pi already had. Typing `/model` or `/effort` into an Agy or OpenCode chat is declined and points at the model picker, the way it is on every other harness. `/fast` is only declined on the harnesses that actually have a fast mode -- on the others the notice would point at a control that is not shown.

Agy chats show which model they are running. The bar is display-only for Agy -- its model is changed from the agent's own terminal, not from the chat -- so it renders as a single, non-clickable name with no effort or speed controls, because Agy has neither: the tier is part of the model name itself. If Agy reports a model newer than the list the workspace ships with, the name is reconstructed from its id so the bar still reads sensibly instead of showing a shrug.

Messages sent to a busy Agy agent now show up as queued instead of being swallowed. Agy only accepts a new message once it has fully finished a turn, and it silently merges anything typed at it mid-turn into a single block -- so a second message would arrive but leave its bubble stuck on "Sending..." forever. The workspace now holds those messages itself and delivers them the moment Agy is free, so what the chat shows matches what Agy actually has.

Stop and shoulder tap work on Agy. Stop ends the current turn and puts everything that had not been sent yet back in the composer, in order, without restarting the agent. Shoulder tap ends the turn and sends the waiting messages straight away instead of waiting for Agy to finish.

The message lifecycle for Agy is covered by the same conservation test the other harnesses have: randomised rounds of sending, stopping and tapping, checking after every round that each message ended up in exactly one place -- delivered, or back in the composer -- never lost and never duplicated.

A message sent to an idle Agy no longer flashes through a queued state. It stayed "Sending…" in principle already -- the backend only marks a message queued when a turn is actually open -- but the chat drew the "Queued messages" header and the shoulder-tap button above it anyway, so a message that was on its way looked parked. The group now appears only when something genuinely is waiting. Codex gets the same fix: its shoulder-tap resend was mislabelled the same way.

A queued message also no longer briefly appears twice as it is delivered. The chip is removed before the turn it became is shown, which was already how the delivery path worked -- but the watcher reads Agy's transcript from two places at once, and the other one usually noticed the new turn first and put it on screen while the chip was still there. That path now waits for the hand-off, so the message is only ever in one state at a time.

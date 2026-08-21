An Agy chat now shows its conversation. The tab was rendering blank -- the harness was registered so it could launch, but nothing read its transcript.

Agy keeps each conversation in its own SQLite database rather than a log file, so the reader tails that store directly, and it is careful about half-written rows: it only advances past a step once that step has settled, and re-reads anything still in progress until it does.

The activity indicator and its caption come with it. Agy records a tool call the moment it dispatches one, with the tool's name and arguments already filled in, so the chat can say "Running python3 showcase.py" while the command is still running rather than after it finishes. One quirk needed handling: Agy writes a short empty "deciding what to do next" step before each tool call, which would otherwise look like a finished answer and make the indicator flicker to idle between every tool.

The model bar is not part of this and still shows nothing for Agy.

Agy's tool results now go through the same handling as every other harness. Permission prompts appear as the usual approval card instead of raw text, so an Agy agent asking for access can actually be answered. Task-tracker bookkeeping commands are hidden as the structural markers they are rather than shown as work. And when a long tool result is shortened for display, the parts the chat reads out of it -- the approval request, the step-tracking lines -- are kept instead of being cut off the end.

The indicator also settles correctly after an interrupted restart. Agy picks its conversation back up where it left off, so a tool call left unfinished by the previous run was still in the store and could leave the indicator running forever; each launch now records when it started, and anything older than that is treated as finished.

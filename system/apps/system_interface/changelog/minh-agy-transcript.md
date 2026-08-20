An Agy chat now shows its conversation. The tab was rendering blank -- the harness was registered so it could launch, but nothing read its transcript.

Agy keeps each conversation in its own SQLite database rather than a log file, so the reader tails that store directly, and it is careful about half-written rows: it only advances past a step once that step has settled, and re-reads anything still in progress until it does.

The activity indicator and its caption come with it. Agy records a tool call the moment it dispatches one, with the tool's name and arguments already filled in, so the chat can say "Running python3 showcase.py" while the command is still running rather than after it finishes. One quirk needed handling: Agy writes a short empty "deciding what to do next" step before each tool call, which would otherwise look like a finished answer and make the indicator flicker to idle between every tool.

The model bar is not part of this and still shows nothing for Agy.

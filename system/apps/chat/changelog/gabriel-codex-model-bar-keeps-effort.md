A Codex chat's model bar now keeps the effort level you pick between messages.

Before, changing the effort between turns was undone within a moment: the bar went back to the effort the previous turn ran at (often none, if that turn ran at the default), while the agent itself was set to the new level. Codex logs every settings change to its session file, and that write made the chat app re-apply the last turn's effort over the pick it had just saved.

The chat app now leaves the saved pick alone until the next turn runs. A turn that falls back to a different model or effort still shows in the bar.

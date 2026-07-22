Added a model picker and fast-mode toggle to the chat composer.

Each chat's composer now has a small control row with a model picker (Fable 5 / Opus 4.8 / Sonnet 5 / Haiku 4.5, defaulting to Opus with its 1M-token context window) and, next to it, a fast-mode toggle. The toggle only appears for models that support fast mode (Opus), and its state reflects whether fast mode is currently enabled.

Picking a model or flipping the toggle applies to the running agent immediately -- it sends the agent a `/model` or `/fast` command, which Claude Code applies live and persists as the agent's default. The picker reads the agent's current selection from its Claude Code settings, so it always shows what the agent is actually using.

The `/model` and `/fast` commands (and Claude Code's confirmation lines) are hidden from the chat transcript so the picker and toggle don't clutter the conversation.

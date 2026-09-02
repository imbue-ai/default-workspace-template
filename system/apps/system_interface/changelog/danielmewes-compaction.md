Render Claude Code context compaction events cleanly in the chat transcript.

- **Status indicator:** Post-compaction summary messages (`isCompactSummary`) are now emitted as subtle status messages ("Context was compacted") rather than as a large user message or expandable chip.
- **Transcript cleanup:** Raw `/compact` invocation commands and local command output from compaction are suppressed from the chat view.
- **Turn boundary and placement:** In the frontend, "Context was compacted" renders as an inline status pill between turns, preserving previous assistant replies and progress blocks rather than absorbing or obscuring them.

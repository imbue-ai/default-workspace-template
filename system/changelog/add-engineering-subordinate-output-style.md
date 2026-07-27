- Chat agents now run a new "Engineering Subordinate" output style: concise,
  direct, anti-sycophantic replies that lead with the outcome, adapt technical
  depth to the reader, and drop preamble/validation filler. It ships as a Claude
  Code output style (`.claude/output-styles/engineering-subordinate.md`, with
  `keep-coding-instructions: true` so coding behavior is retained). Claude Code
  folds the full style text into the system prompt once per session and
  re-asserts it each turn via its built-in per-turn style reminder, so the tone
  holds across long chats without re-sending the whole ruleset.

- The style is scoped to user-facing chat only. A new `[agent_types.chat]`
  (parent_type `claude`) carries `outputStyle` through `settings_overrides`
  (which accumulates onto the parent, so `model`/`fastMode` are preserved), and
  `[create_templates.chat]` now targets it. Worktree, worker, and services
  (`main`) agents keep their existing types and are unaffected, so delegated and
  background agents still report in full technical detail. Both the initial
  `/welcome` agent and every "New Agent" chat launch with `--template chat`, so
  both pick up the style.

# data/.state/

Machine state the workspace services read and write:

- `apps.toml` - The registry of running apps and their ports.
- `oom_priority/` - The memory-pressure shed ledger and agent-pid registry.
- `browser-fleet.json`, `browser-screenshots/` - Shared browser service state.
- `initial_chat_created` - First-boot marker for the welcome chat.
- `last-restic-prune` - Backup maintenance timestamp.
- `isolated-instances/` - State for temporarily booted app instances.
- `user_knowledge.toml` - Whether the user has already used permissions, sharing, inspirations, the browser, or personalizing the agent, so the agent doesn't proactively suggest a feature they already know about. Each table also carries `times_suggested`/`last_suggested_at` so the agent backs off after repeated ignored suggestions.
  - `[inspirations]` `has_published`/`has_used` -- written by `publish-inspiration`/`use-inspiration` on success.
  - `[sharing]` `has_shared_workspace` -- written by `share-workspace`, only after it confirms `data/.secrets/share.env` is actually present (never self-reported).
  - `[browser]` `has_used` -- written by `agentic-browser-fleet` after a successful browser action.
  - `[permissions]` -- no `has_*` field; the agent checks latchkey's live permission state instead (see the latchkey skill) rather than trusting a cached flag that could go stale (e.g. a permission later revoked).
  - `[personalization]` -- no `has_*` field either; the agent checks live whether CLAUDE.md or `.agents/skills/` differs from the pristine template base (same OLDEST-marker walk `publish-inspiration` uses to find `BASE_REF`), since personalization can happen via direct edits with no dedicated skill to hook.

- New **user knowledge tracking**: `data/.state/user_knowledge.toml` records whether the
  user has already used permissions, sharing, inspirations, the browser, or personalizing
  their agent, plus how many times and when the agent last proactively suggested each
  one. CLAUDE.md now instructs the agent to check this file before suggesting one of
  these, and to record the suggestion when it makes one.

- `publish-inspiration` and `use-inspiration` now set `has_published`/`has_used` under
  `[inspirations]` in that file once their action actually completes (alongside their
  existing `docs/VERSION_HISTORY.md` entry). `agentic-browser-fleet` sets `[browser]
  has_used` after its first real action in a session.

- New **`share-workspace`** skill: answers "how does sharing work" (it's enabled from the
  desktop client, not from chat), checks the live signal (`data/.secrets/share.env`) for
  whether sharing is currently active rather than trusting the user's say-so, and records
  `has_shared_workspace` in the user-knowledge file once observed active.

- `[permissions]` and `[personalization]` intentionally have no self-reported `has_*`
  field -- the agent checks live instead (latchkey's permission state; whether CLAUDE.md
  or `.agents/skills/` differs from the pristine template base), since a locally cached
  flag could go stale (a permission later revoked) or miss changes made with no
  dedicated skill to hook (a direct CLAUDE.md edit).

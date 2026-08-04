- New **user knowledge tracking**: `data/.state/user_knowledge.toml` records whether the
  user has already used permissions, sharing, or inspirations, plus how many times and
  when the agent last proactively suggested each one. CLAUDE.md now instructs the agent
  to check this file (and latchkey's live permission state) before suggesting one of
  these features, and to record the suggestion when it makes one.

- `publish-inspiration` and `use-inspiration` now set `has_published`/`has_used` under
  `[inspirations]` in that file once their action actually completes (alongside their
  existing `docs/VERSION_HISTORY.md` entry).

- New **`share-workspace`** skill: answers "how does sharing work" (it's enabled from the
  desktop client, not from chat), checks the live signal (`data/.secrets/share.env`) for
  whether sharing is currently active rather than trusting the user's say-so, and records
  `has_shared_workspace` in the user-knowledge file once observed active.

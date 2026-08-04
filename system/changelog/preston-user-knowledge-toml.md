- CLAUDE.md and `data/.state/README.md` now document `data/.state/user_knowledge.toml`,
  machine state tracking whether the user has already used permissions, sharing, or
  inspirations, so the agent knows not to keep proactively suggesting a feature the user
  already knows about. See the `.agents` changelog entry for the skill-side changes that
  write to this file.

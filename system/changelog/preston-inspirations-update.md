- Workspaces now ship **`docs/VERSION_HISTORY.md`** -- a plain, human-readable
  record of where the workspace came from and what it has published or adopted.
  A `## Workspace` section holds the template version it was created from plus
  one line per update; an `## Inspirations` section holds one entry per
  published inspiration (`v1`, `v2`, ... under a per-slug heading); an
  `## Adopted inspirations` section holds the version of each inspiration this
  mind adopted from a remote. Each line ends in the commit it was cut from,
  earlier lines are never rewritten, so the whole lineage is walkable in git.
  Each skill that writes it (`update-self`, `publish-inspiration`,
  `update-published-inspiration`, `update-installed-inspiration`) carries its own instructions
  for exactly what to append -- there is no separate helper skill.

- Added design docs under `docs/system/blueprint/agent-inspiration-update-awareness/` for
  knowing an inspiration's status and updating a published one: the full
  proposal, plus a short summary of the version-history file and of why an
  update re-runs the inspiration's recipe against the current workspace rather
  than diffing two repositories (which is what preserves deliberate exclusions).

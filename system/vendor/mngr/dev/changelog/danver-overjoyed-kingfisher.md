Audited the supply-chain cooldown exceptions -- the per-package overrides that let a
dependency skip the repo's 2-week minimum release age -- and retired the one that had
aged out.

Removed the `modal` `[tool.uv] exclude-newer-package` override from the root
`pyproject.toml` and the public-mirror overlay, advancing both `exclude-newer` cutoffs
forward-only from 2026-07-20 to 2026-09-01 (today minus the two-week
`DEPENDENCY_COOLDOWN`, the value `scripts/release.py` would compute at the next
release). The override existed only because modal 1.5.4 (published 2026-08-12)
postdated the old cutoff; with the cutoff past it, the override was dead config.
Relocking `uv.lock` and `mirror/overlay/uv.lock` changes no package version -- the
diff is confined to the recorded `[options]` metadata, and modal stays pinned at 1.5.4.

Kept the two pnpm `minimumReleaseAgeExclude` entries in `apps/minds/pnpm-workspace.yaml`
(`latchkey`, `@imbue-ai/detent`). Both are Imbue-published packages we intend to adopt
on release rather than after a cooldown: `latchkey@3.13.0` is one day old and
`@imbue-ai/detent` is its transitive dependency, so dropping either entry would break
the next `pnpm install` that re-resolves.

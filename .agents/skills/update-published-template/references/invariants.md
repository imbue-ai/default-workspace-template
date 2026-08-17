# Invariants an update must preserve

Every one of these also holds for a first publish. They are restated here
because an update is the flow most likely to erode them by accident: it
starts from already-published content rather than a clean base.

- **Bootable-or-nothing.** Every published version, including this update, is the
  full bootable tree; a failed step publishes nothing.
- **Preserve the user's customizations.** The update re-assembles from the
  PUBLISHED TIP and overlays only the approved delta -- the finished manifest
  prose, "## Recipe", thumbnail, `/welcome`, and adopters' "Adaptation history"
  are never regenerated. `build_template.sh` is never run for an update.
- **One atomic post-cleanup commit.** The mint is a single `commit-tree` from the
  final, scanned, generalized tree, parented on the published tip -- no pre-scan
  or pre-generalization state ever exists as its own commit, and `merge-base(template, tip)`
  stays `BASE_REF`.
- **Private-by-default; visibility never changes on an update**, and the final
  gate restates it.
- **The hard secret scan is the authoritative blocker** -- re-run over every
  overlaid/modified path, hard-failing.
- **Both chat gates run** -- the §2e scope gate and the §5 final gate; no earlier
  approval substitutes for either.

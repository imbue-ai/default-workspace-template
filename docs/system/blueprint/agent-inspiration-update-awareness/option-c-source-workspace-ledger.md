# Version history (simple .md)

An inspiration is not a fork of the workspace -- it is **derived** from it by a
**recipe**: include these paths, exclude this, strip this personal data. So an
update is never a diff between the two repos: you **re-run the recipe against the
current workspace** and push the result as a new version, carrying the published
repo's manifest / thumbnail / adaptation-history forward unchanged.

Alongside that, keep ONE human-readable `VERSION_HISTORY.md` -- plain dated lines,
each ending in the workspace commit that version was cut from. That sha is
**provenance** (and lets an agent optionally show "what changed since v1" =
`git diff <v1-sha>..HEAD -- <paths>`, within the workspace repo, for the status
message). It is not an input to building the update -- the recipe is.

## Example file

```
# Version history

## Workspace
- 2026-05-02  created from minds-v0.3.6   a1b2c3d
- 2026-07-20  updated to minds-v0.3.8     9f8e7d6

## Inspirations

### people-crm  --  github.com/preston/people-crm
- v1  2026-06-15  first published                      c0ffee1
- v2  2026-07-25  synced the bug fix in sync.py        deadbee
```

## How lines get added

- **Self-update:** `update-self` appends one `## Workspace` line with the merge sha.
- **Publish:** appends `### <slug>` + `- v1  <date>  first published  <source-sha>`.
- **Update an inspiration:** appends `- v<n+1>  <date>  <one line: what changed>  <source-sha>`.

Each append is its own commit.

## Exclusions persist across updates

Because an update re-runs the recipe (not the raw workspace), anything you excluded
stays excluded even though it is still in your workspace:

- **Excluded path:** simply not in the include set, so a re-run can never pull it in.
- **Excluded feature inside an included path:** the recipe strips it again on every
  re-run; the scope gate defaults to "still exclude."
- **Adding a new app later** is a separate scope-gate addition to the include set;
  the old exclusion is untouched.

This needs the inspiration to record its recipe -- include paths plus what was
stripped -- set at publish and amended only when the scope changes. One small recipe
per inspiration, not per version. An inspiration is a bootable snapshot, so it always
reflects the real current state of the included paths minus the recipe's exclusions,
never a hand-picked mix of edits.

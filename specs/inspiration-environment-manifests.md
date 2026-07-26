# Note for a future spec: inspiration environment manifests

Written alongside the user-data-layout / env-converge work; to be picked up by
the workspace-root-relayout + inspiration-manifest restructuring effort.

## Motivation

Inspirations (publish-inspiration / use-inspiration) currently declare their
runtime needs only as prose plus `requires_permission:` / `requires_secret:` /
`requires_llm:` lines in `inspiration-<slug>.md`. System packages an
inspiration's code needs are implicit -- the adopting mind discovers them by
running into failures. With env-converge in place, the environment side has a
principled home for these declarations.

## Direction agreed so far

- Split the machine-readable parts of the manifest out of the markdown into a
  pydantic-validated `inspiration-<slug>.toml` (prose, holes, and the two
  append-only history logs stay in the .md). Validation runs at publish time
  in `build_inspiration.sh`'s gate, not just as instructions to the worker.
- Add per-inspiration declared dependency sections mirroring the environment
  record's shape: `apt` (name-level pins as the portable default; the
  publisher's snapshot timestamp recorded as provenance), `npm_global`,
  `uv_tools`, and `binaries`/`script` entries for exotic installs (which
  become `scripts/env.d/` units carried by the inspiration's tree).
- Adoption converges the union: `use-inspiration` installs every adopted
  inspiration's declared set at the ADOPTER's pinned snapshot timestamp (union
  semantics across multiple adopted inspirations is the natural merge). A
  package that does not resolve at the adopter's timestamp surfaces as a
  `package_unavailable` event with an explicit "run the upgrade" prompt
  (timestamp skew), not a silent failure.
- Publish-time validation checks every declared apt package resolves in the
  mirrored universe at the publisher's timestamp, so unmirrorable third-party
  sources are rejected (or trigger mirroring) at the earliest moment.
- Composition should become file-addition rather than file-merge where cheap:
  per-inspiration env.d units and (future) supervisord `[include]` drop-ins
  instead of edits to shared files.

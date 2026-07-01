- Added **inspirations**: a publishable, reusable snapshot of the apps and
  features a mind has built. A mind can publish an inspiration as its own clean
  GitHub repo, and another mind can adapt one into itself. A single repo can
  accumulate several inspirations over time (one `inspiration-<name>.md`
  manifest per inspiration at the repo root).

- New **`/publish-inspiration`** skill. It asks what to include (no user data by
  default), then delegates the assembly to a `launch-task` sub-agent on an
  isolated worktree. The worker resets to the clean FCT version the mind was
  based on (no upstream fetch -- provenance link only), overlays only the
  selected paths, runs a hard-failing secret scan (aborts before commit on any
  token/credential), generates the manifest + a placeholder SVG thumbnail,
  rewrites the `/welcome` stable region, and does a side-effect-free boot check.
  The user then confirms/edits the title, description, repository name,
  visibility, and thumbnail in a popup before the lead creates the repo and
  pushes.

- New **`/use-inspiration`** skill. Brings an existing inspiration into the
  current mind -- either as the template a new mind is created from (the rewritten
  `/welcome` drives it on startup), or by merging one in from a git URL -- then
  fills in the inspiration's "holes" interactively with the user and records what
  was adapted back into the manifest.

- New **system_interface publish popup**: a box in the workspace UI (backed by
  `/api/inspiration/*`) that previews the proposed inspiration and lets the user
  edit the fields before publishing. The SVG thumbnail is sanitized before it is
  previewed or committed.

- New **system_interface GitHub-login modal** (backed by `/api/github-auth/*`):
  a one-click GitHub login (web/device flow or a pasted token) for users without
  an in-VM `GH_TOKEN`, so publishing can push. It configures gh's credential
  store and the git credential helper in place -- no agent restart is needed
  (unlike the Claude API-key flow, the credential is only needed at `git push`
  time). All new credential/inspiration endpoints are restricted to loopback
  callers.

- Added a one-sentence note in `CLAUDE.md` that inspirations exist. Publishing
  is user-initiated; the agent does not proactively push the user to create one.

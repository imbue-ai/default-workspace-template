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

- Made inspiration assembly fast and reliable. The secret scan in
  `build_inspiration.sh` now scans only the paths overlaid out of the live mind
  (the selected apps/data plus carried-forward manifests) instead of the whole
  assembled tree; the clean FCT base is trusted and public, so scanning it only
  produced false positives on its own test-fixture placeholder tokens (e.g.
  `sk-ant-test`) that blocked every publish. The token patterns now also require
  a realistic key length after each prefix, so short placeholder values do not
  fire, and the single-pass scan no longer traverses `vendor/mngr` or the base's
  fixtures. The boot smoke-check now runs on the interpreter that already ships
  the supervisor library (the installed `supervisord` binary's shebang) rather
  than `uv run`, which had to resolve and build the entire project environment
  just to parse `supervisord.conf` -- slow on a cold base and prone to spurious
  aborts on unrelated build errors. Assembly now runs directly in a local
  throwaway `git worktree` in the same container instead of a `launch-task`
  sub-agent, which added minutes of latency for a sub-second job without adding
  isolation.

- Fixed the in-mind GitHub-login modal so it can actually persist a credential.
  The system_interface process inherits `GH_TOKEN` from the agent environment,
  and `gh` prioritizes `GH_TOKEN` / `GITHUB_TOKEN` (and enterprise variants) over
  its credential store -- so `gh auth login` refused to store the pasted/web
  credential and `gh auth status` reported the env token, and the modal never
  wrote anything durable. Every `gh` call in the GitHub-auth backend now runs
  with those variables scrubbed from the child environment (the parent process
  environment is untouched), and the publish skill scrubs them for its own
  `gh auth status` probe and final `gh repo create --push` too.

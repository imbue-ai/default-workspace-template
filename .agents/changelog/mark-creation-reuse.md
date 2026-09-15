# Reuse what already exists

- `fetch-process-show` now claims the case where the ask names the surface
  rather than the data ("visualize my roadmap from the export I uploaded"), and
  `build-app` sends an app that reads records from outside itself there before
  it plans anything. An app whose ingestion was hand-written inside `build-app`
  has no entry point anyone can re-run when the next batch of data lands.

- `update-self`'s plan tests read the set of app tools off the tree but spelled
  out the template's five when asserting what a shared manifest refreshes, so
  the file failed in any workspace where the user had built an app. The
  assertion now reads the same set it derives.

- The milestone path in `lead-proxy.md` now says the lead does the tab refresh
  itself, and that the worker's report body is written for the lead rather than
  for the user. The same point already landed on the `done` path in
  `launch-task`; the milestone message never got it.

- `build-app` and `update-app` name the design skill by its full
  `frontend-design:frontend-design` form. It ships as a plugin, so the bare name
  they used before resolves only when the agent guesses the prefix -- and when it
  does not, the design pass they call mandatory is silently skipped.

- `use-ai-integration` points at the `claude_p.py` it actually ships. Its prose
  had sent readers to `system/scripts/claude_p.py`, a path that has never existed
  in this repo, so an agent following the skill spends its first minutes hunting
  for the helper. The migrate-workspace note that documented the discrepancy is
  gone with it.

- `lead-proxy.md` opens by saying none of its machinery reaches the user. Leases,
  polls, milestones, merges, gates and worker names are how work gets done, not
  what got done, and the quality judge scored six trials on status lines like
  "Fresh, no lease. Merging the milestone" that name exactly that plumbing.

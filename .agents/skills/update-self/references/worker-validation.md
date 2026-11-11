# Worker: validation items (the worker guide's Step 4b)

Read this when any condition in the worker guide's Step 4b table holds beyond
the skill-exemption stamp. Run each item whose condition holds, in this
order, and no other. Record which items ran and why, with each condition and
whether it held, in your report.

## Contents

- Environment gate
- Suites, lint, ratchets
- Isolated-service boots
- Playwright
- Customization survival

## Environment gate

**Runs when a manifest or lockfile is in the merged set. First.**

- At the root: `uv lock --check`, then `uv sync --all-packages`.
- For each user package (`python3
  system/scripts/workspace_bare_scripts/user_packages.py list`; each is a
  standalone project with its own lock): `uv lock --check --project
  <package>`.

A failure here is a precise blocker: an unparseable root lock means no
service in the workspace can start. Fix it before running anything else.

- A user package whose lock is stale after the merge (a shared library it
  uses or the mngr pin moved) is relocked with `uv run --no-sync
  user-package-migration relock <package>`, never by hand-editing its lock.
- A user package that is not a standalone project
  (`system/test_user_packages_are_standalone.py` names it), or a failure here
  or in the suites below on the workspace's own skills, apps, services or
  docs that the update's changelog ties to a changed rule: convert or fix it
  as `older-workspaces.md` ("Python from before the packaged layout") says.

## Suites, lint, ratchets

**Runs for what the merged set, the update's changes to the user's
creations, 4a's impacted consumers, and your own edits can reach, and for
nothing else.** With none of them, no suite runs at all.

The test selector names the suites from those paths: each changed package's
or skill's own tests, the suites of whatever declares a dependency on it, the
frontend checks, the repo guards, and the full root suite for a path in no
package or skill or in a package the root project depends on. A set made only
of documentation selects nothing.

What goes in:

- The merged set.
- The update's changes inside a creation that carries local content (every
  creation whose scope files, `.local.json` and `.update.json`, both list
  files inside its footprint). These go in by rule, so the user's own tests
  of it run whether or not a file changed on both sides.
- What 4a found impacted by search or reasoning, through
  `impacted-paths.txt`. No file means 4a found none, or did not run.
- Your own edits. Commit them first: the `git diff` line reads commits.

```bash
eval "$(uv run --no-sync .agents/shared/scripts/agents_shared/parse_task_frontmatter.py 'data/.tasks/update-self/task.md')"
MERGE="$(git log --merges -1 --format=%H --fixed-strings \
    --grep "update-self: merge upstream template ($TARGET_REF)")"
python3 data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py \
    classify-merge --local "$MERGE^1" --target "$TARGET_REF" \
    > data/.tasks/update-self/classify.json
{ jq -r '.merged[].path' data/.tasks/update-self/classify.json
  for update in data/.tasks/update-self/scopes/*.update.json; do
      [ -e "$update" ] || continue
      jq -e '.diff.inside_footprint | length > 0' "${update%.update.json}.local.json" >/dev/null \
          && jq -r '.diff.inside_footprint[]' "$update"
  done
  cat data/.tasks/update-self/impacted-paths.txt 2>/dev/null
  git diff --name-only --no-renames "$MERGE" HEAD; } | sed '/^[[:space:]]*$/d' | sort -u \
    > data/.tasks/update-self/validate-paths.txt
[ -s data/.tasks/update-self/validate-paths.txt ] \
    && uv run --frozen --package app-manifest app-manifest select-tests \
        --diff-base "$MERGE^1" \
        $(sed 's/^/--path /' data/.tasks/update-self/validate-paths.txt)
```

`--diff-base` here only names what a merged `uv.lock` is compared against, so
a lock that upgraded a package selects the suites that depend on it.

Run every printed line, in order, plus `uv run --no-sync ruff check` when a
Python file is in the list. Handle a failing test and a shed command the way
`data/.tasks/update-self/skill-at-target/.agents/skills/harden-worker/references/harden-creation.md`
("The test gate") says, naming a flaky test under your report's Validation.
The tree before your change, for attributing a failure, is `$MERGE^1` (a test
absent there arrived with the update). mngr's own suite runs in its repo, not
here.

## Isolated-service boots

**Runs for each service with a file in the merged set, and for each service
4a found impacted that carries local content of its own**: one the workspace
created, or a built-in one it has modified (a file of it in `local_only`).

Boot it against a scratch data copy through
`.agents/shared/scripts/agents_shared/serve_isolated_instance.py` (the
`update-app` skill describes it), never the live store.

A service that arrived exactly as upstream shipped it is not booted here:
upstream tested it, and for the shell and the chat app the apply pre-flights
the merged copy itself before anything live restarts. Every other built-in
program that arrived clean lands on upstream's testing alone, caught after
the restart only by the apply's probes and rollback; that is the coverage
this rule gives up, on purpose. A boot runs on the host's global toolchain,
so it does not exercise a global-dependency bump.

## Playwright

**Runs for a web surface (the shell or a user service) only when the merge
needed nontrivial merge work there.**

For the shell, build the frontends in your worktree: `uv sync
--all-packages`, then `cd system && npm ci && npm run build`. One npm
workspace emits all seven bundles: the shell's, the chat app's, the Getting
Started app's, the Files app's, the Routines app's, the Screen's, and the
Style Guide's. Drive it per
`data/.tasks/update-self/skill-at-target/.agents/skills/harden-worker/references/web-frontend-testing.md`.

Those seven bundles are what the lead's apply installs live
(`--worker-bundle`, one per app, installed only as a set), so name all seven
locations in your report.

## Customization survival

**Runs for every user creation the update touches**: workspace-added apps,
widgets and skills; user-modified built-in surfaces; apps hooking into the
shell's API or state.

For a manifest app that set is read off the scope files: every creation
whose `.local.json` and `.update.json` both list files inside its footprint,
checked over the files the `.update.json` names. A creation 4a found coupled
through an interface joins it whether or not its footprint shows a change.

Verify the *merged result* still carries each one in substance. Suites
passing is not the bar. For a visual surface, screenshot the merged instance
you booted and the running workspace's surface (read-only) for the before
picture, and actually look at the pair. For an app or integration, exercise
its hook points. Classify each:

- **Intact.**
- **Intact-but-changed** (moved, restyled). Never blocks. Record it with the
  before/after evidence, so the lead can offer to restore the old
  arrangement.
- **Cannot be kept**: no place for it on the new base, or broken and your
  attempts to re-fit it failed ("tried and failed", never "looks hard"). This
  is the one verdict that stops the pass: raise it as a `question` gate (the
  worker guide's Step 6) with the evidence and options, and never let it ride
  into `done`. A conflict where every resolution breaks the creation lands
  here too.

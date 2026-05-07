---
name: submit-upstream-changes
description: Push local improvements to shared infrastructure (skills, scripts, CLAUDE.md scaffolding, Dockerfile, services.toml) back to the parent template repo so other agents derived from the template benefit. Opens a separate per-feature PR per logical fix; never pushes directly to upstream `main`. Do not push agent-specific content (PURPOSE.md, memory, runtime state). For pulling updates from upstream, use the `update-self` skill instead.
---

# Pushing changes upstream

This repo was created from a parent template repo (see `parent.toml` for the upstream URL and branch). The default flow for pushing improvements back is: **one logical fix per PR, on a `submit/<short-name>` branch**. We do not push directly to upstream `main`.

## What to push (and what not to)

Push **shared infrastructure** that benefits other agents derived from the template:

- Skills (`.agents/skills/`)
- Scripts (`scripts/`, `.agents/shared/scripts/`)
- CLAUDE.md scaffolding (template-level sections only)
- Dockerfile
- `services.toml` (template-level entries)

Do **not** push agent-specific content:

- `PURPOSE.md`
- Memory contents
- Runtime state (`runtime/`)
- Agent-specific services, settings, or CLAUDE.md sections

## Pre-flight: GraphQL rate limit

`gh pr create` and `gh repo view` go through the GraphQL API, which has a per-user 5000/hour quota shared across the org. Mid-session it can already be exhausted, and `gh pr create` then fails with an opaque "API rate limit already exceeded". Check first:

```bash
gh api rate_limit --jq '.resources.graphql | "remaining=\(.remaining) reset=\(.reset)"'
```

If `remaining` is 0 (or single-digit and you have several PRs to open), stop. Format the reset epoch for the user and wait. The recipe uses Python rather than `date -d "@..."` because the latter is GNU-only and fails on macOS:

```bash
RESET=$(gh api rate_limit --jq '.resources.graphql.reset')
python3 -c "
import datetime, sys
ts = int(sys.argv[1])
print('GraphQL quota resets at',
      datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
              .strftime('%Y-%m-%dT%H:%M:%SZ'))
" "$RESET"
```

Do not retry-loop; surface the reset time and hand back to the user.

## PR conventions

- **Branch name:** `submit/<short-feature-name>` (kebab-case, ~3-5 words). Same name on the upstream remote.
- **One logical fix per PR.** Multiple commits are fine if they form one logical unit; otherwise split them across PRs so each can be reviewed/CI'd/merged independently.
- **Title:** short, imperative, scoped. e.g. `forwarder: redirect HTTPS by default`.
- **Body:** a single paragraph explaining the *why* (the motivating bug, missing capability, or constraint). Reviewers can read the diff for the *what*. Skip checklists and section headers.
- **Co-Authored-By trailer** on the commit (the standard one used in this repo).

## Recipe

The upstream URL and base branch are in `parent.toml`.

1. Ensure the `upstream` remote points at the template (idempotent):

   ```bash
   git remote get-url upstream 2>/dev/null || git remote add upstream "$(python3 -c "
   import tomllib
   with open('parent.toml', 'rb') as f:
       print(tomllib.load(f)['url'])
   ")"
   ```

2. Stage the commit(s) you want to push onto a clean throwaway branch rooted at upstream's base, then push that branch. Pushing the local working branch directly (`git push upstream <local_branch>:submit/<short-name>`) would publish every ancestor commit not yet on upstream -- including unrelated WIP, merge, and scaffolding commits that happen to share the branch tip's history -- producing a noisy PR that violates the "one logical fix per PR" rule. Instead:

   ```bash
   git fetch upstream
   BASE=$(python3 -c "
   import tomllib
   with open('parent.toml', 'rb') as f:
       print(tomllib.load(f)['branch'])
   ")
   git branch -f submit/<short-name> "upstream/$BASE"
   git checkout submit/<short-name>
   git cherry-pick <sha-1> [<sha-2> ...]   # the commit(s) for this logical fix, oldest first
   git push upstream submit/<short-name>:submit/<short-name>
   git checkout -   # back to your working branch
   ```

   If the cherry-pick conflicts against current upstream, resolve it the same way you would for any cherry-pick (or rebase your fix on a fresh `update-self` first).

3. Open the PR against the template's default branch (read from `parent.toml`, usually `main`):

   ```bash
   gh pr create \
       --repo imbue-ai/forever-claude-template \
       --base main \
       --head submit/<short-name> \
       --title "<short imperative title>" \
       --body  "<one-paragraph why>"
   ```

4. Report the PR URL back to the user.

## When to push

- When the user asks you to push changes upstream.
- After improving shared skills, scripts, or configuration that would benefit other agents.

## Important

- Always commit your local changes before pushing.
- Double-check the diff: `git show <sha>` -- make sure no agent-specific content is in the commit.
- One upstream PR per logical fix. Don't bundle.
- We do **not** push directly to upstream `main`. If you genuinely need to (e.g. a one-off `parent.toml`-driven sync, with explicit user instruction), the existing `parent.toml` lookup still gives you the URL and branch -- but treat it as the rare exception, not the default.
- To pull updates from upstream, use the `update-self` skill.

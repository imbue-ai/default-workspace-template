Made the changelog gate stack-aware: the CI workflow now resolves a PR's live base branch from the GitHub API into `CHANGELOG_DIFF_BASE_REF` (which `scripts/check_changelog_entries.py` prefers over `GITHUB_BASE_REF`), because for PRs in a native GitHub stack the webhook payload reports the trunk as base, which made the gate diff the whole stack and demand entries for lower layers' projects.

The changelog gate's CI job also checks out the PR head instead of the default merge ref, so trunk drift since a stack's base point no longer shows up as files the PR touches.

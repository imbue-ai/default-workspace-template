The changelog gate now takes a PR's base branch from the event payload instead
of the runner's `GITHUB_BASE_REF`.

On a stacked PR the two were observed to disagree -- the payload named the
parent branch while the env var said `main`. The gate could then not resolve the
base it was told to use, fell back to `origin/main`, and diffed the whole stack
rather than the one PR, failing it for changelog entries belonging to the PRs
underneath.

The failure mode was the bad kind: a green run and a red run on the same
unchanged files, which reads as flakiness rather than as a diff computed against
the wrong base.

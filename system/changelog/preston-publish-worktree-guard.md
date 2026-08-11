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

Two halves, because the first fix was not enough. The job now takes the base
from the event payload rather than the runner's variable, and it fetches the
base into `FETCH_HEAD` and places the remote-tracking ref with `git update-ref`
-- passing a `+src:dst` refspec was observed to succeed while creating no ref
at all. And the gate itself now refuses an unresolvable named base instead of
quietly falling through to `main`, which is what turned a misresolved ref into
what looked like a flaky check.

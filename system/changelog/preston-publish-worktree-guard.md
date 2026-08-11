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

The base now travels in `CHANGELOG_BASE_REF`, set from the event payload.
GitHub reserves the `GITHUB_` prefix, so a workflow declaring
`env: GITHUB_BASE_REF:` has it echoed in the log and then ignored -- the
runner's own value wins, and on a stacked PR that value was `main` rather than
the parent branch. Overriding it was therefore impossible under that name.

The job also places the remote-tracking ref with `git update-ref` and verifies
it, rather than trusting a `+src:dst` refspec, which was observed to succeed
while creating no ref at all. And the gate refuses an unresolvable named base
instead of falling through to `main`: falling back turned a wrong base into a
confident wrong answer, which is what made this read as a flaky check across
two runs of identical files.

# Recording the v(n+1) entry in the ledger

The full recording contract for `update-published-inspiration` §8. It runs
ONLY after the push succeeded -- an update that did not publish is never
recorded.

The single sanctioned write back to `/home/user/workspace` -- read the CWD-INVARIANT callout at
the top before running it. If the push failed or the user aborted, SKIP this
entirely: an update that did not publish is never recorded.

Write the entry directly into `docs/VERSION_HISTORY.md` (cwd `/home/user/workspace`) -- the same
`## Inspirations` recording contract `publish-inspiration` §8 step 4 owns, just
computing the NEXT version instead of v1. Append-only; every `## Inspirations`
line ends in a commit; a retried step is a no-op, never a duplicate. Inputs:
`SLUG=<slug>`, `REPO_URL="github.com/<owner>/<repo>"`, `NOTE="<one line: what
changed>"`, and `SOURCE_SHA` = the current `/home/user/workspace` HEAD the update was cut from
(the source anchor for v(n+1) -- NOT `BASE_REF`, NOT `PUBLISHED_TIP`, NOT anything
from `$WT`).

- The slug's `### <slug>  --  <repo-url>` heading already exists (this mind
  published v1 through `publish-inspiration`). In the unlikely event
  `docs/VERSION_HISTORY.md` is missing, recreate the shipped three-section
  starter (`## Workspace`, `## Inspirations`, `## Adopted inspirations`; the exact
  heredoc lives in `update-self` §5b) and re-add the heading before appending.
- Append one line under that heading:

  ```
  - v<n+1>  <today, YYYY-MM-DD>  <NOTE>  <7-char SOURCE_SHA>
  ```

  where `<n+1>` is **computed**, never typed: one greater than the highest `v<k>`
  already listed under this slug's heading. Pad the note to width 35 so the sha
  lines up; compute the sha as `git rev-parse --short=7 "$SOURCE_SHA"`.
  **Idempotence, scoped to this slug:** if a line already under this slug's
  heading carries this exact note AND this exact 7-char sha, it is already
  recorded -- change nothing and skip the commit.

Then commit that one file:

```bash
( cd /home/user/workspace \
    && git add docs/VERSION_HISTORY.md \
    && git commit -m "version history: updated inspiration <slug> to v(n+1)" )
```

Exactly one file staged by name, one commit, on whatever branch `/home/user/workspace` is on.
NEVER `git add -A`, never a merge/checkout/reset. If the idempotence check found
the entry already recorded, nothing is staged and you skip the commit. If the
commit fails (a hook rejects it), the update still succeeded -- say so and fix the
entry rather than re-pushing anything.


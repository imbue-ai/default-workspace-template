# Auditing the worker's `done` report (SKILL.md Step 5a)

There is no approval gate: the audit, not the user, authorizes the apply. The
worker's impact analysis, validation scope and review gates are decided by
rules (its guide's Steps 4a, 4b and 4c), and its report must show which
branch of each rule applied, with the evidence. `worker-report.md` lists the
sections a `done` body carries; you do not need the rest of the worker's
guide to audit one.

## What the report must show

- **The 4a and 4b branches.** The footprint evidence: `has_local_footprint`,
  and when it is true, each creation's scope-file verdicts. Each validation
  item's condition, and whether it held.
- **The 4c branch**, one of:
  - the clean-pull skip's three conditions: `has_merge_work: false`; no
    impacted user-created code; and no in-branch edits of the worker's own
    beyond its Step 1 rollback reverts (as git made them, or with a `both
    added` file taken at the target's version) and its skill-exemption commit
    (`Record skill-check exemptions`, touching only skills' frontmatter),
    shown by an empty diff against the landed merge on each side of that
    commit;
  - the gate run's own evidence: the fix commits kept or reverted, or a clean
    run, plus the architecture-gate verdicts.
- **A side-picked conflict** carries the discarded-side accounting.
- **The retired names** it searched for, when the impact analysis ran: the
  `retired-names` listing and what each search found.

## A `done` that answers a catch-up

It carries a **Catch-up** section, and the audit reads it the same way: it
must show which of the items of the worker guide's Step 7 ran over the commits
it brought in, each with its evidence, and how each conflict there was
resolved. The worker's earlier evidence for the rest of the pass still
stands. The clean-pull skip's empty diff then runs to the branch's tip before
its first catch-up merge, not to its tip (`worker-review-gates.md`).

## A report with a gap

A report missing any of this, including one that openly discloses skipping a
gate outside the rule, goes back to the worker: reply saying what is missing
and re-arm the poll, as `question-gates.md` ("Answering and re-arming") does.
Do not run the apply over the gap. A deviation stands only when the worker is
gone and the gap cannot be closed from here, and then the results message
states it plainly as a caveat.

## After the audit

The `done` report is your raw material, not the user's message: the results
message is composed *after* the apply, per `results-message.md`.

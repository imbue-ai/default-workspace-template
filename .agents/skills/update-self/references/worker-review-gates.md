# Worker: the review-gate rule

Whether the review gates run on an update merge is decided by a rule, not by
judgment. Apply it and record which branch applied, with its evidence, in the
report.

## Skip the gates only on a pure clean pull

All three must hold:

1. Step 4's `classify-merge` reports `has_merge_work` false -- an empty merged
   set: no conflicts, no file changed on both sides, no lockfile you
   regenerated.
2. Your 4a impact analysis found no user-created code (apps, skills, local
   scripts) depending on anything the update changed, and no global-dep bump
   with a user-created dependent. Built-in impacts do not block the skip.
   When the 4a rule skipped the analysis, `has_local_footprint` false is this
   condition's evidence: there is no user-created code to depend on anything.
3. You authored no in-branch edits of your own. A 4a mirror edit, or any
   other commit you added on top of the merge, is merge work even though
   `classify-merge` (which diffs `HEAD^1` against the base) cannot see it.
   The commits that are not are the rollback reverts Step 1 mandates, as git
   made them: they put earlier updates' own content back. A revert conflict
   you resolved is your edit, unless you took the target's version of a
   `both added` file. `git diff <merge-sha> HEAD` must still be empty, where
   `<merge-sha>` is this pass's merge or, on a retry of a release already in
   history (whose merge adds no commit), that release's landed merge. Name the
   merge, each revert sha, and any revert conflict with how you resolved it
   in the report as this condition's evidence. A catch-up merge (the worker
   guide's §7) is not an edit of yours: what it brings in is the workspace's
   own work. A conflict you resolved in it is.

Every changed file then arrives exactly as upstream shipped and tested it, and
there is nothing local for a review to protect. Running `/autofix` here would
review *upstream's* code and could apply local fixes to it -- manufacturing
exactly the local divergence a future update would have to reconcile -- so on
a clean pull the skip is the correct outcome, not a shortcut. The report states
that this branch fired and shows the evidence for all three conditions.

## Otherwise run the real gates, scoped to the locally-divergent content

Follow the "Review gates" section of
`.agents/shared/worker/references/harden-creation.md`, with one difference in
its test gate: 4b's suites step already ran it. That step's `select-tests`
read every path in this scope (the merged set, your own edits, a regenerated
lockfile) and more, so do not select again with another base. Never run
`select-tests --diff-base "$TARGET_REF"`: that diff is everything the workspace
ever changed, not what this update reconciled, and it repeats suites 4b already
ran. What you commit after 4b's suites run (a customization re-fit, a gate
fix) is tested by what it reaches: `select-tests --diff-base <the commit
before the first of them>`, every line of it.

The gate's scope is **every file whose merged content differs from the target
release**: the conflicts you resolved with any hand-written content, your own
in-branch edits, and any lockfile you regenerated. A file byte-identical to
the release arrived exactly as upstream tested it, and a fix to it would only
manufacture local divergence. Over an 800-file release this is the difference
between reviewing four reconciled files and reviewing upstream's code. Name
the scope you ran in your report. Widening it is always allowed; narrowing it
below that set, or substituting a review of your own design, is not -- this
rule already *is* the proportionality decision. "The merge is dominated by
upstream-tested code" licenses the skip branch when its conditions hold, and
licenses nothing when they do not.

**Disposition of fix commits.** The test is the file's merged **content**, not
which set `classify-merge` put it in: **keep fixes to a file whose content
differs from the release** (one you reconciled by hand, or edited in-branch
yourself even though `classify-merge` lists it as pulled-in) and **revert fixes
to a file that is still byte-identical to the release** (note them as
`submit-upstream-changes` candidates instead), including a conflicted file you
resolved by taking upstream wholesale. The gate's job is the reconciliation and
local breakage, not improving upstream's code.

## The escape hatch

If you believe the gates should not run -- or should run at some other scope
-- in a situation this rule does not cover, that is a `question` gate for the
lead (Step 6), never a silent adaptation, however well-reasoned and however
openly you would have disclosed it. The lead answers it by this rule; where
the rule is genuinely silent, the fallback is more coverage, never less.

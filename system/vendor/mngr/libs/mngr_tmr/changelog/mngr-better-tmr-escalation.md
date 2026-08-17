Reworked TMR escalations so a run's escalations are reviewable. A recent run raised 445 of them across 343 tests, which the integrator's own grouping showed to be 21 underlying problems; the report presented all 445 as one flat list, and the pull request dropped its escalation table entirely for exceeding GitHub's body limit.

Changed: escalation kinds now name the work rather than what happened to the reporting agent. `BLOCKER` and `SHARED_PATTERN` (which put 93% of escalations in one bucket) are replaced by `UNCAUGHT_BUG`, `FIX_DIRECTION_AMBIGUOUS`, `HARNESS_DEFECT`, and `SUITE_DUPLICATION`. `kind` is now required: a missing one used to parse silently as `BLOCKER`, the most severe category.

Changed: an escalation carries a single `description_markdown` (whose first line is a one-sentence summary) instead of a separate title and detail, plus `locations` naming the paths and lines it concerns, so the report can point at the code rather than only at the test whose agent noticed.

Changed: the integrator reports one `escalations` array whose entries group the mappers' escalations by underlying problem, each carrying `member_ids` and, when it fixed the problem, `resolved_in_commit_hash`. The separate `normalizations` field is gone: a suite-wide cleanup is a resolved escalation the integrator raised to itself.

Added: `python -m imbue.mngr_tmr.escalation_coverage` checks that every mapper escalation belongs to one of the integrator's groups, so the grouping relationship is enforced rather than requested. The integrator runs it before publishing, and the report re-checks at render time and says so in the page when a gap remains.

Changed: the HTML report now shows unresolved escalations in full and resolved ones one line each, both as real sections with sidebar entries, under a leading integration report. The raw per-mapper escalations are always listed, whether or not the integrator has run -- the report is regenerated on every poll, and mappers finish long before the integrator exists.

Changed: report sections are renamed and reordered. `IMPL_FIXES` is now matched before the test-fix branch, so an agent that fixed the implementation and its test is no longer filed under a test-only label; `NON_IMPL_FIXES` becomes `TEST_AND_DOC_FIXES` (matching the `[TEST/DOC]` commit the reducer already writes); `UNRESOLVED` becomes `FIX_FAILED`, freeing that word for escalations; and results fitting no section land in an explicit `INDETERMINATE` rather than masquerading as failed fixes.

Changed: the pull request body leads with `Full report: <url>`, then the mapper status breakdown, then unresolved escalations in full and resolved ones in a table. Its title counts unresolved integrator escalations rather than raw mapper reports, which had produced "429 escalated" for a run holding 21 problems.

Changed: mapper prompts cap escalation prose (120 words for a suite duplication, 250 otherwise) and drop the "escalating costs you nothing" framing, while telling agents explicitly not to withhold escalations -- many agents reporting one problem is the signal that surfaces it.

"""Check that the integrator's escalation groups cover every mapper escalation.

The integrator's escalations are groupings of the mappers': many agents describe
one problem in their own words, and the grouping is what makes the problem
legible. That relationship is only useful if it is total -- if a mapper
escalation belongs to no group, it is missing from the grouped view without
anything saying so.

Asking the integrator to be thorough does not make the relationship hold. It
reads hundreds of escalations in one pass, and a prompt cannot verify itself.
So the check is mechanical and runs twice: the integrator invokes this module
before it publishes, so it can fix its own gaps, and ``report.py`` re-derives the
same set at render time so a run that skipped the step still shows the gap.

What this can and cannot prove: it proves every escalation is *claimed* by some
group. It cannot prove the grouping is sound -- an integrator could satisfy it by
sweeping the tail into one catch-all group. The report shows each group's member
count for that reason, so an oversized catch-all is visible rather than merely
conformant.

Usage (from the reducer prompt)::

    python -m imbue.mngr_tmr.escalation_coverage <inputs_dir> --reducer-outcome <path>
"""

import argparse
import sys
from pathlib import Path

from imbue.mngr_tmr.pr_summary import collect_results
from imbue.mngr_tmr.report import load_integrator_outcome_file
from imbue.mngr_tmr.report import ungrouped_escalation_ids


def find_ungrouped_ids(inputs_dir: Path, reducer_outcome_path: Path) -> list[str]:
    """Mapper escalation ids that no integrator escalation claims as a member.

    Delegates to ``report.ungrouped_escalation_ids``, which is what the report
    itself calls, so the reducer's check and the rendered page cannot disagree
    about which escalations are grouped. This function's own job is only to read
    the reducer's on-disk layout into the rows that helper takes.

    An unreadable outcome counts as no groups at all, so every escalation comes
    back ungrouped. That is the true state of affairs when the reducer has not
    written its outcome yet, and it is what makes running this before writing
    the file a loud failure rather than a silent pass.
    """
    outcome = load_integrator_outcome_file(reducer_outcome_path)
    return ungrouped_escalation_ids(
        collect_results(inputs_dir),
        outcome.escalations if outcome is not None else (),
    )


def main(argv: list[str]) -> int:
    """Print any ungrouped escalation ids and exit non-zero when there are some."""
    parser = argparse.ArgumentParser(
        description="Check that every mapper escalation belongs to an integrator escalation group."
    )
    parser.add_argument("inputs_dir", type=Path, help="Directory of per-mapper output directories")
    parser.add_argument(
        "--reducer-outcome",
        type=Path,
        required=True,
        help="Path to the integrator's own outcome file",
    )
    args = parser.parse_args(argv[1:])

    # A mistyped inputs path would otherwise read zero outcomes, find zero
    # ungrouped ids, and report a confident pass -- the one failure mode a check
    # like this must not have.
    if not args.inputs_dir.is_dir():
        print(f"No such inputs directory: {args.inputs_dir}")
        return 2
    results = collect_results(args.inputs_dir)
    if not results:
        print(f"No mapper outcomes found under {args.inputs_dir}; nothing to check.")
        return 2

    ungrouped = find_ungrouped_ids(args.inputs_dir, args.reducer_outcome)
    if not ungrouped:
        print(f"All mapper escalations across {len(results)} outcome(s) are covered by an escalation group.")
        return 0
    print(f"{len(ungrouped)} mapper escalation(s) belong to no group:")
    for ungrouped_id in ungrouped:
        print(f"  {ungrouped_id}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))

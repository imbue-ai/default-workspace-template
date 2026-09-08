"""The scripted half of `harness_quality`: score each scope on how many harness failure signatures its
trajectory contained, with no model in the loop.

The judges beside these criteria read prose and can weigh "hit a missing plugin, tried twice, worked
around it" against "gave up and shipped nothing". That judgement is worth having, and it is also
arguable. These counts are not: a run whose tool output says `Unknown skill:` said it, however well the
agent coped afterwards. Scoring both means a harness regression shows up even in the runs where the
agent papered over it.

The score decays as ``half_marks_at / (half_marks_at + count)``: 1.0 for a clean scope, half marks at
the configured count, approaching but never reaching zero. It never reaches zero on purpose -- a
trajectory is a sample of what the harness did, and no finite count of signatures proves that nothing
worked. Decay also keeps the criterion discriminating at the bad end, where a linear budget would floor
every badly-broken run at the same 0. Counts come from harness_failures.json, written by
render_harness_report.py in the same pre-step pass.

Runs in the verifier container: stdlib + rewardkit only, absolute paths.
"""

import json
from pathlib import Path
from typing import Any

from rewardkit import criterion

FAILURE_COUNTS_PATH = Path("/logs/agent/harness_failures.json")
DEFAULT_HALF_MARKS_AT = 4


def _failure_counts(counts_path: Path = FAILURE_COUNTS_PATH) -> dict[str, Any] | None:
    try:
        loaded = json.loads(counts_path.read_text())
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def score_for(total: int, half_marks_at: int) -> float:
    """1.0 for a clean scope, half marks at ``half_marks_at`` failures, approaching zero beyond it.

    ``half_marks_at`` is sanitized to a positive int by the caller, since a curve through zero has no
    meaning and scoring 0.0 there would charge a trial for a grading fault.
    """
    return float(half_marks_at) / float(half_marks_at + max(0, total))


def scope_score(scope: str, counts_path: Path = FAILURE_COUNTS_PATH) -> float:
    """The score for one scope's failure count, read from the counts file the pre-step wrote."""
    counts = _failure_counts(counts_path)
    if counts is None:
        # The pre-step did not run or wrote something unreadable. That is a grading fault rather than a
        # trial fault, and finalize.py is the place that voids a trial -- scoring 0 here would blame the
        # run for the harness's own failure to measure it, which is the exact confusion this dimension
        # exists to remove.
        return 1.0
    scope_counts = counts.get(scope)
    if not isinstance(scope_counts, dict):
        return 1.0
    recorded_half_marks_at = counts.get("half_marks_at")
    half_marks_at = (
        recorded_half_marks_at
        if isinstance(recorded_half_marks_at, int) and recorded_half_marks_at > 0
        else DEFAULT_HALF_MARKS_AT
    )
    # scored_total leaves out the signatures that are evidence but not breakage (an agent probing for a
    # path it does not find). A report written before that split carries only `total`.
    total = scope_counts.get("scored_total", scope_counts.get("total"))
    if not isinstance(total, int):
        # A non-numeric count is a broken report, not a broken run, and a criterion that raises aborts
        # the whole grade -- score it clean and let the reports say what happened.
        return 1.0
    return score_for(total, half_marks_at)


@criterion(description="How soundly the lead agent's own skills, plugins and tooling held up")
def main_harness_soundness(workspace: Path) -> float:
    """How sound the lead agent's harness was: its failure-signature count on the decay curve."""
    return scope_score("main")


@criterion(description="How soundly the launched workers' skills, plugins and tooling held up")
def worker_harness_soundness(workspace: Path) -> float:
    """How sound the workers' harness was: every captured worker's failure-signature count, summed,
    on the decay curve.

    A trial that launched no worker scores 1.0: there was no worker harness to break.
    """
    return scope_score("workers")

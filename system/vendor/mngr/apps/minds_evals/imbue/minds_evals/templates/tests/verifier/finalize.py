"""Compose the final gated reward from rewardkit's dimension scores.

rewardkit writes reward.json with the per-dimension scores ({"gates": ..., "quality": ..., and
"outcome" when the case declares expectations}) and reward-details.json with per-criterion results.
The intended trial reward is "what the agent earned, zeroed unless every structural gate passed",
which rewardkit's reward.toml aggregations cannot express -- so this step adds the "reward" key (the
score harbor parses) and stamps a timed_out marker into reward-details.json.

Cases that declare expectations split that earned score evenly between conversation quality and the
delivered outcome: a great app described badly and a great description of no app are equally
imperfect. Cases without expectations are unchanged.

Grading-infrastructure failures must NOT be graded as a legitimate 0.0: they leave the reward file
absent so harbor errors the trial instead. That covers a judge API/auth error, rewardkit not
producing a parseable reward file, a case file that does not say what this case expects, and outcome
evidence that could not be measured at all (an expectations case that finished with an absent or
empty evidence manifest, or a declared check class whose every recorded entry is an error).
rewardkit soft-handles a judge timeout by recording the criterion as 0.0 with an ``error`` and
exiting 0, which would otherwise masquerade as a real low score.

Short of that, a *partially* errored class still scores -- over its surviving entries -- so this step
also stamps an ``outcome_evidence`` marker into reward-details recording how complete the
measurement was. Without it a trial whose bridge flaked is indistinguishable from a fully-measured
one, transient errors bias outcome scores upward, and a bridge-reliability regression reads as an
agent improvement.
"""

import json
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

# harbor's contract, not this harness's choice: the file harbor parses for the trial reward, and
# rewardkit's own output location.
REWARD_PATH = Path("/logs/verifier/reward.json")
# rewardkit's per-criterion results, written beside the reward.
DETAILS_PATH = Path("/logs/verifier/reward-details.json")
# Under /logs/agent, harbor's contract for where a task's declared artifacts are re-materialized at
# their original absolute paths.
STATE_PATH = Path("/logs/agent/state.json")
# /tests is harbor's contract for where the task's tests directory lands in the container.
CASE_PATH = Path("/tests/case.json")
# The evidence bundle: a declared artifact, so it re-materializes under /logs/agent too.
MANIFEST_PATH = Path("/logs/agent/verification/manifest.json")

# The headline knob: how much of a gated trial's reward the delivered artifact carries. Constant in
# v1, deliberately not per-case -- per-case weights would make rewards incomparable across cases.
OUTCOME_SHARE = 0.5

# Which expanded check list makes a class scored, mirroring outcome/checks.py's registration rule.
SCORED_CLASS_BY_EXPECTATION_KEY = {"files_checks": "files", "app_checks": "app", "http_checks": "http"}

# ui_flow_checks is deliberately NOT in that map. An unmeasurable inventory or registry means the
# collection phase itself failed, which is worth erroring a trial over. A flow set where every
# entry errored usually means only that the flow executor was unavailable -- no browser, no
# forward proxy, a dead tunnel -- and voiding the trial would discard its conversation-quality
# measurement over one of them. outcome/checks.py registers no ui_flows criterion in that
# case, so the flows contribute nothing in either direction, and the manifest still records which
# part broke.


def _reward_dicts(dimension: Any) -> list[dict[str, Any]]:
    """The per-reward detail dicts for one dimension. rewardkit emits a single dict when a dimension
    directory yields one Reward, or a list of dicts when it yields several (e.g. a judge .toml plus
    programmatic .py files), so both shapes must be handled."""
    if isinstance(dimension, dict):
        return [dimension]
    if isinstance(dimension, list):
        return [entry for entry in dimension if isinstance(entry, dict)]
    return []


def _criteria(reward_dict: dict[str, Any]) -> Iterable[dict[str, Any]]:
    return (entry for entry in reward_dict.get("criteria", []) if isinstance(entry, dict))


def _gates_all_passed(details: dict[str, Any]) -> bool:
    reward_dicts = _reward_dicts(details.get("gates"))
    if not reward_dicts:
        return False
    saw_criterion = False
    for reward_dict in reward_dicts:
        for criterion in _criteria(reward_dict):
            saw_criterion = True
            if criterion.get("value", 0) <= 0:
                return False
    return saw_criterion


def _judge_error(details: dict[str, Any]) -> str | None:
    """The first judge/criterion error across all dimensions, or None. rewardkit records an ``error``
    on any criterion whose judge call failed (timeout, auth, rate limit) after zeroing its value."""
    for dimension in details.values():
        for reward_dict in _reward_dicts(dimension):
            for criterion in _criteria(reward_dict):
                error = criterion.get("error")
                if error:
                    return str(error)
    return None


def _load_json(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _is_timed_out(state_path: Path) -> bool:
    state = _load_json(state_path)
    return state.get("test_state") == "timed_out" or bool(state.get("timed_out"))


def _is_conversation_finished(state_path: Path) -> bool:
    return _load_json(state_path).get("test_state") == "finished"


class UntrustworthyCaseFileError(Exception):
    """The case file cannot say what this case expects: absent, unparseable, or not of the shape the
    generator writes. Local to this script because the verifier container has no ``imbue`` package."""


def _load_expectations(case_path: Path) -> dict[str, Any]:
    """The case's expanded expectations, or an empty dict for a case that declares none.

    The one strict reader in this script: the generator writes case.json into every task, so a
    file that cannot be read as the shape it writes is a broken harness, and raises
    UntrustworthyCaseFileError rather than degrading to "no expectations" (which would grade a
    commissioned deliverable quality-only). ``expectations`` absent or null is the genuine bare case.
    """
    try:
        loaded = json.loads(case_path.read_text())
    except OSError as exc:
        raise UntrustworthyCaseFileError(
            "cannot read the case file {} ({})".format(case_path, exc.strerror or exc)
        ) from exc
    except ValueError as exc:
        raise UntrustworthyCaseFileError("the case file {} is not valid JSON ({})".format(case_path, exc)) from exc
    if not isinstance(loaded, dict):
        raise UntrustworthyCaseFileError(
            "the case file {} holds {}, not a JSON object".format(case_path, type(loaded).__name__)
        )
    expectations = loaded.get("expectations")
    if expectations is None:
        return {}
    if not isinstance(expectations, dict):
        raise UntrustworthyCaseFileError(
            "the case file {} declares expectations as {}, not an object".format(
                case_path, type(expectations).__name__
            )
        )
    return expectations


def _manifest_entries(manifest_path: Path) -> list[dict[str, Any]]:
    entries = _load_json(manifest_path).get("entries")
    return [entry for entry in entries if isinstance(entry, dict)] if isinstance(entries, list) else []


def _evidence_marker(manifest_path: Path) -> dict[str, Any] | None:
    """What the manifest says about how completely this trial was measured, or None without one.

    Stamped into reward-details because a partially-errored class scored over its surviving entries
    is otherwise indistinguishable from a fully-measured one: transient bridge errors would bias
    outcome scores upward, and a bridge-reliability regression would read as an agent improvement.
    Read-only -- it records what happened, it does not change any score.
    """
    if not manifest_path.is_file():
        return None
    manifest = _load_json(manifest_path)
    entries = _manifest_entries(manifest_path)
    error_count_by_class: dict[str, int] = {}
    for entry in entries:
        if entry.get("status") == "error":
            check_class = str(entry.get("check_class") or "unknown")
            error_count_by_class[check_class] = error_count_by_class.get(check_class, 0) + 1
    return {
        "is_evidence_complete": bool(manifest.get("is_evidence_complete")),
        "entry_count": len(entries),
        "error_count_by_class": error_count_by_class,
    }


def _evidence_failure(
    rewards: dict[str, Any], expectations: dict[str, Any], state_path: Path, manifest_path: Path
) -> str | None:
    """Why this expectations case's outcome could not be measured at all, or None if it could.

    The signal is manifest.json, never the directory: the driver creates verification/ unconditionally
    at setup so that harbor can always collect the declared artifact, which means an absent directory
    can no longer mean anything. A manifest that is absent or empty on a trial reporting ``finished``
    means the collection phase never ran, which is the harness's failure to measure. On an unfinished
    or timed-out trial, partial-or-absent evidence is expected -- the structural gates already zero it.
    """
    if not expectations:
        return None
    if not _is_conversation_finished(state_path):
        return None
    if not manifest_path.is_file():
        return "the case declares expectations and the conversation finished, but no evidence bundle was collected"
    entry_list = _manifest_entries(manifest_path)
    if not entry_list:
        return "the case declares expectations and the conversation finished, but the evidence manifest is empty"
    if "outcome" not in rewards:
        return "the outcome dimension produced no score"
    for expectation_key, check_class in SCORED_CLASS_BY_EXPECTATION_KEY.items():
        if not expectations.get(expectation_key):
            continue
        determinable = [
            entry for entry in entry_list if entry.get("check_class") == check_class and entry.get("status") != "error"
        ]
        if not determinable:
            return "no determinable {} evidence was recorded for a declared {} expectation".format(
                check_class, check_class
            )
    return None


def _earned_reward(rewards: dict[str, Any], expectations: dict[str, Any]) -> float:
    quality = float(rewards.get("quality", 0.0))
    if not expectations:
        return quality
    outcome = float(rewards.get("outcome", 0.0))
    return (1.0 - OUTCOME_SHARE) * quality + OUTCOME_SHARE * outcome


def _fail_as_grading_error(reason: str, reward_path: Path) -> int:
    """Leave no parseable reward file so harbor errors the trial rather than grading a fake 0.0."""
    print("finalize: grading infrastructure failure: {}".format(reason), file=sys.stderr)
    try:
        reward_path.unlink()
    except OSError:
        pass
    return 1


def finalize(reward_path: Path, details_path: Path, state_path: Path, case_path: Path, manifest_path: Path) -> int:
    """Compose the gated reward from the files at these paths; the exit code for the verifier."""
    try:
        rewards = json.loads(reward_path.read_text())
        details = json.loads(details_path.read_text())
    except (OSError, ValueError) as exc:
        return _fail_as_grading_error("cannot read rewardkit outputs ({})".format(exc), reward_path)

    # Unconditional, and before the gates: the case file is part of the task, not of the run, so
    # nothing about how the trial went can make a broken one acceptable.
    try:
        expectations = _load_expectations(case_path)
    except UntrustworthyCaseFileError as exc:
        return _fail_as_grading_error(str(exc), reward_path)

    judge_error = _judge_error(details)
    if judge_error is not None:
        return _fail_as_grading_error("judge call failed ({})".format(judge_error[:200]), reward_path)

    # Outcome evidence is only load-bearing on a trial whose gates passed; a timed-out trial scores
    # zero on structure alone and must not be reported as a harness failure.
    is_gated_open = _gates_all_passed(details)
    if is_gated_open:
        evidence_failure = _evidence_failure(rewards, expectations, state_path, manifest_path)
        if evidence_failure is not None:
            return _fail_as_grading_error(evidence_failure, reward_path)

    rewards["reward"] = round(_earned_reward(rewards, expectations) if is_gated_open else 0.0, 4)
    reward_path.write_text(json.dumps(rewards, indent=2))

    details["timed_out"] = _is_timed_out(state_path)
    evidence_marker = _evidence_marker(manifest_path)
    if evidence_marker is not None:
        details["outcome_evidence"] = evidence_marker
    details_path.write_text(json.dumps(details, indent=2))
    return 0


def main() -> int:
    return finalize(
        reward_path=REWARD_PATH,
        details_path=DETAILS_PATH,
        state_path=STATE_PATH,
        case_path=CASE_PATH,
        manifest_path=MANIFEST_PATH,
    )


if __name__ == "__main__":
    sys.exit(main())

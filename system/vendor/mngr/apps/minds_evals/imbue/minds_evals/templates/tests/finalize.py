"""Compose the final gated reward from rewardkit's dimension scores.

rewardkit writes reward.json with the per-dimension scores ({"gates": ..., "quality": ...}) and
reward-details.json with per-criterion results. The intended trial reward is "quality, zeroed unless
every structural gate passed", which rewardkit's reward.toml aggregations cannot express -- so this
step adds the "reward" key (the score harbor parses) and stamps a timed_out marker into
reward-details.json.

Grading-infrastructure failures (a judge API/auth error, or rewardkit not producing a parseable
reward file at all) must NOT be graded as a legitimate 0.0: they leave the reward file absent so
harbor errors the trial instead. rewardkit soft-handles a judge timeout by recording the criterion
as 0.0 with an ``error`` and exiting 0, which would otherwise masquerade as a real low score.
"""

import json
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

REWARD_PATH = Path("/logs/verifier/reward.json")
DETAILS_PATH = Path("/logs/verifier/reward-details.json")
STATE_PATH = Path("/logs/agent/state.json")


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


def _is_timed_out() -> bool:
    try:
        state = json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return False
    return isinstance(state, dict) and (state.get("test_state") == "timed_out" or bool(state.get("timed_out")))


def _fail_as_grading_error(reason: str) -> int:
    """Leave no parseable reward file so harbor errors the trial rather than grading a fake 0.0."""
    print("finalize: grading infrastructure failure: {}".format(reason), file=sys.stderr)
    try:
        REWARD_PATH.unlink()
    except OSError:
        pass
    return 1


def main() -> int:
    try:
        rewards = json.loads(REWARD_PATH.read_text())
        details = json.loads(DETAILS_PATH.read_text())
    except (OSError, ValueError) as exc:
        return _fail_as_grading_error("cannot read rewardkit outputs ({})".format(exc))

    judge_error = _judge_error(details)
    if judge_error is not None:
        return _fail_as_grading_error("judge call failed ({})".format(judge_error[:200]))

    quality = rewards.get("quality", 0.0)
    gated_reward = float(quality) if _gates_all_passed(details) else 0.0
    rewards["reward"] = round(gated_reward, 4)
    REWARD_PATH.write_text(json.dumps(rewards, indent=2))

    details["timed_out"] = _is_timed_out()
    DETAILS_PATH.write_text(json.dumps(details, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

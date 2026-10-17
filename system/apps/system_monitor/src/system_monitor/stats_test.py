"""Tests for the snapshot every open window shares."""

from typing import Any

from system_monitor import stats


def test_a_shared_snapshot_is_reused_within_its_age_and_taken_again_after() -> None:
    computed: list[int] = []
    now = [100.0]

    def compute() -> dict[str, Any]:
        computed.append(1)
        return {"n": len(computed)}

    shared = stats.SharedSnapshot(compute, max_age=4.0, clock=lambda: now[0])

    assert shared.get() == {"n": 1}
    now[0] += 3.9
    assert shared.get() == {"n": 1}
    now[0] += 0.2
    assert shared.get() == {"n": 2}
    assert len(computed) == 2

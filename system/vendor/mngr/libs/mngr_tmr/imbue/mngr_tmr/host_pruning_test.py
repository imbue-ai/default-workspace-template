"""Unit tests for selecting which past runs' hosts may be destroyed."""

from datetime import datetime
from datetime import timedelta
from datetime import timezone

from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr_mapreduce.cli import RUN_NAME_LABEL_KEY
from imbue.mngr_tmr.host_pruning import group_agents_by_run
from imbue.mngr_tmr.host_pruning import select_prunable_run_names
from imbue.mngr_tmr.testing import make_agent_details

_NOW = datetime(2026, 8, 26, 6, 0, tzinfo=timezone.utc)
_MIN_AGE = timedelta(hours=12)


def _make_run_agents(variant: str, run_name: str, host_count: int, create_time: datetime) -> list[AgentDetails]:
    """Build one mapper agent per host of a run, named the way the framework names them."""
    return [
        make_agent_details(
            name=f"{variant}-{run_name}-test-something-{index}",
            host_name=f"{variant}-{run_name}-host-{index}",
            create_time=create_time,
            labels={RUN_NAME_LABEL_KEY: run_name},
        )
        for index in range(host_count)
    ]


def test_agents_group_into_one_entry_per_run() -> None:
    agents = [
        *_make_run_agents("tmr-mngr", "20260825080921", 2, _NOW - timedelta(days=1)),
        *_make_run_agents("tmr-minds", "20260825090000", 1, _NOW - timedelta(days=1)),
    ]
    runs = group_agents_by_run(agents)
    assert [(run.variant, run.run_name, run.agent_count) for run in runs] == [
        ("tmr-minds", "20260825090000", 1),
        ("tmr-mngr", "20260825080921", 2),
    ]
    assert runs[1].host_names == ("tmr-mngr-20260825080921-host-0", "tmr-mngr-20260825080921-host-1")


def test_agents_without_a_run_label_are_ignored() -> None:
    agents = [
        make_agent_details(
            name="hand-made-agent",
            host_name="someones-laptop",
            create_time=_NOW,
            labels={"project": "mngr"},
        )
    ]
    assert group_agents_by_run(agents) == ()


def test_the_run_of_a_reducer_host_groups_with_its_mappers() -> None:
    """The reducer and snapshotter hosts carry the same <variant>-<run>- prefix as the pool hosts."""
    agents = [
        *_make_run_agents("tmr-mngr", "20260825080921", 1, _NOW - timedelta(days=1)),
        make_agent_details(
            name="tmr-mngr-20260825080921-reducer",
            host_name="tmr-mngr-20260825080921-reducer",
            create_time=_NOW - timedelta(days=1),
            labels={RUN_NAME_LABEL_KEY: "20260825080921"},
        ),
    ]
    runs = group_agents_by_run(agents)
    assert len(runs) == 1
    assert runs[0].variant == "tmr-mngr"
    assert runs[0].agent_count == 2


def test_newest_run_of_each_variant_is_kept() -> None:
    runs = group_agents_by_run(
        [
            *_make_run_agents("tmr-mngr", "20260820080921", 1, _NOW - timedelta(days=6)),
            *_make_run_agents("tmr-mngr", "20260825080921", 1, _NOW - timedelta(days=1)),
            *_make_run_agents("tmr-minds", "20260819090000", 1, _NOW - timedelta(days=7)),
            *_make_run_agents("tmr-minds", "20260824090000", 1, _NOW - timedelta(days=2)),
        ]
    )
    prunable = select_prunable_run_names(
        runs,
        now=_NOW,
        kept_run_count_per_variant=1,
        minimum_prunable_age=_MIN_AGE,
    )
    assert prunable == ("20260819090000", "20260820080921")


def test_a_variant_whose_only_run_is_ancient_is_still_kept() -> None:
    """A suite that has not run for weeks keeps its last run, so it stays debuggable."""
    runs = group_agents_by_run(_make_run_agents("tmr-minds", "20260701090000", 1, _NOW - timedelta(days=56)))
    prunable = select_prunable_run_names(
        runs,
        now=_NOW,
        kept_run_count_per_variant=1,
        minimum_prunable_age=_MIN_AGE,
    )
    assert prunable == ()


def test_a_run_younger_than_the_minimum_age_is_kept() -> None:
    """A run that started hours ago may still be going, even with a newer run behind it."""
    runs = group_agents_by_run(
        [
            *_make_run_agents("tmr-mngr", "20260826010000", 1, _NOW - timedelta(hours=5)),
            *_make_run_agents("tmr-mngr", "20260826020000", 1, _NOW - timedelta(hours=4)),
        ]
    )
    prunable = select_prunable_run_names(
        runs,
        now=_NOW,
        kept_run_count_per_variant=1,
        minimum_prunable_age=_MIN_AGE,
    )
    assert prunable == ()


def test_a_run_name_shared_by_two_variants_is_kept_while_either_keeps_it() -> None:
    runs = group_agents_by_run(
        [
            *_make_run_agents("tmr-mngr", "20260825080921", 1, _NOW - timedelta(days=1)),
            *_make_run_agents("tmr-mngr", "20260820080921", 1, _NOW - timedelta(days=6)),
            *_make_run_agents("tmr-minds", "20260820080921", 1, _NOW - timedelta(days=6)),
        ]
    )
    prunable = select_prunable_run_names(
        runs,
        now=_NOW,
        kept_run_count_per_variant=1,
        minimum_prunable_age=_MIN_AGE,
    )
    assert prunable == ()


def test_keeping_more_than_one_run_per_variant() -> None:
    runs = group_agents_by_run(
        [
            *_make_run_agents("tmr-mngr", "20260823080921", 1, _NOW - timedelta(days=3)),
            *_make_run_agents("tmr-mngr", "20260824080921", 1, _NOW - timedelta(days=2)),
            *_make_run_agents("tmr-mngr", "20260825080921", 1, _NOW - timedelta(days=1)),
        ]
    )
    prunable = select_prunable_run_names(
        runs,
        now=_NOW,
        kept_run_count_per_variant=2,
        minimum_prunable_age=_MIN_AGE,
    )
    assert prunable == ("20260823080921",)

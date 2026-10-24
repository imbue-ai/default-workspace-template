import sqlite3
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from imbue.mngr_usage.model_call import ModelCallEvent, price_model_call

from usage_collector.limits import LimitReading
from usage_collector.store import TimeBucket, UsageStore, UsageStoreError
from usage_collector.testing import NOW, model_call_payload


def _event(**overrides: object) -> ModelCallEvent:
    return price_model_call(
        ModelCallEvent.model_validate(model_call_payload(**overrides))
    )


def test_insert_is_idempotent_by_event_id(store: UsageStore) -> None:
    event = _event()

    assert store.insert(event)
    assert not store.insert(event)

    [row] = store.totals([], NOW - timedelta(hours=1))
    assert row.calls == 1


def test_rollup_and_raw_totals_agree(store: UsageStore) -> None:
    for origin_id in ("files", "files", "routines"):
        store.insert(_event(attribution={"origin_id": origin_id, "harness": "litellm"}))

    from_rollups = store.totals(["origin_id"], NOW - timedelta(hours=1))
    from_raw = store.totals(["origin_id", "harness"], NOW - timedelta(hours=1))

    assert [
        (r.group["origin_id"], r.calls, r.cost_usd, r.tokens) for r in from_rollups
    ] == [(r.group["origin_id"], r.calls, r.cost_usd, r.tokens) for r in from_raw]
    assert from_rollups[0].group == {"origin_id": "files"}
    assert from_rollups[0].calls == 2
    assert from_rollups[0].tokens["input"] == 2000


def test_a_group_whose_calls_all_lack_a_cost_reports_unknown_not_zero(
    store: UsageStore,
) -> None:
    store.insert(_event(model="anthropic/claude-not-real-4471"))
    store.insert(_event())

    rows = {
        r.group["model"]: r for r in store.totals(["model"], NOW - timedelta(hours=1))
    }

    unpriced = rows["anthropic/claude-not-real-4471"]
    assert unpriced.cost_usd is None and unpriced.unknown_cost_calls == 1
    priced = rows["anthropic/claude-opus-5-5"]
    assert priced.cost_usd is not None and priced.cost_usd > Decimal(0)


def test_missing_dimensions_read_back_as_none(store: UsageStore) -> None:
    store.insert(_event())

    [row] = store.totals(["agent_id"], NOW - timedelta(hours=1))

    assert row.group == {"agent_id": None}


def test_totals_bucket_by_hour_and_respect_the_window(store: UsageStore) -> None:
    store.insert(_event())
    store.insert(
        _event(
            ended_at=(NOW - timedelta(hours=3)).isoformat(),
            started_at=(NOW - timedelta(hours=3)).isoformat(),
        )
    )

    rows = store.totals([], NOW - timedelta(hours=5), bucket=TimeBucket.HOUR)
    recent = store.totals([], NOW - timedelta(hours=1))

    assert [row.bucket_start.hour for row in rows if row.bucket_start is not None] == [
        9,
        12,
    ]
    assert recent[0].calls == 1


def test_unknown_group_by_is_refused(store: UsageStore) -> None:
    with pytest.raises(UsageStoreError):
        store.totals(["prompt"], NOW)


def test_prune_drops_old_raw_events_but_keeps_rollups(store: UsageStore) -> None:
    old = (NOW - timedelta(days=91)).isoformat()
    store.insert(
        _event(started_at=old, ended_at=old, attribution={"harness": "litellm"})
    )

    assert store.prune(NOW) == 1
    assert store.totals(["harness"], NOW - timedelta(days=100)) == []
    assert store.totals(["origin_id"], NOW - timedelta(days=100))[0].calls == 1


def test_an_event_pruned_from_raw_storage_is_not_counted_again(
    store: UsageStore,
) -> None:
    old = (NOW - timedelta(days=91)).isoformat()
    event = _event(started_at=old, ended_at=old)
    store.insert(event)
    store.prune(NOW)

    assert not store.insert(event)
    assert store.totals([], NOW - timedelta(days=100))[0].calls == 1


def test_a_database_from_before_counted_ids_still_skips_its_stored_events(
    tmp_path: Path,
) -> None:
    path = tmp_path / "old.db"
    event = _event()
    old_store = UsageStore(path)
    old_store.insert(event)
    old_store.close()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE counted_event")

    reopened = UsageStore(path)
    try:
        assert not reopened.insert(event)
        assert reopened.totals([], NOW - timedelta(hours=1))[0].calls == 1
    finally:
        reopened.close()


def test_file_positions_persist(tmp_path: Path, store: UsageStore) -> None:
    path = tmp_path / "events.jsonl"
    assert store.file_position(path) == (None, 0)

    store.set_file_position(path, 42, 1000)
    store.set_file_position(path, 42, 1500)

    assert store.file_position(path) == (42, 1500)


def test_summed_costs_carry_no_float_noise(store: UsageStore) -> None:
    for _ in range(3):
        store.insert(_event(cost_usd="0.1", cost_provenance="REPORTED"))

    [row] = store.totals([], NOW - timedelta(hours=1))

    assert row.cost_usd == Decimal("0.3")


def _timed(seconds: float, **overrides: object) -> ModelCallEvent:
    return _event(
        started_at=(NOW - timedelta(seconds=seconds)).isoformat(),
        ended_at=NOW.isoformat(),
        **overrides,
    )


def test_latency_percentiles_leave_out_untimed_calls_and_count_them(
    store: UsageStore,
) -> None:
    for seconds in (1, 2, 3, 4, 10):
        store.insert(_timed(seconds))
    store.insert(_timed(0))

    [row] = store.latency([], NOW - timedelta(hours=1))

    assert (row.timed_calls, row.untimed_calls) == (5, 1)
    assert (row.p50_ms, row.p95_ms) == (3000, 10000)


def test_latency_counts_log_timed_calls_as_approximate_per_group(
    store: UsageStore,
) -> None:
    store.insert(_timed(2, attribution={"harness": "claude"}))
    store.insert(_timed(1, model="anthropic/claude-haiku-4-5"))

    rows = {
        row.group["model"]: row
        for row in store.latency(["model"], NOW - timedelta(hours=1))
    }

    assert rows["anthropic/claude-opus-5-5"].approximate_calls == 1
    assert rows["anthropic/claude-haiku-4-5"].approximate_calls == 0
    assert rows["anthropic/claude-haiku-4-5"].p50_ms == 1000


def test_a_group_of_only_untimed_calls_has_no_percentiles(store: UsageStore) -> None:
    store.insert(_timed(0))

    [row] = store.latency([], NOW - timedelta(hours=1))

    assert (row.timed_calls, row.untimed_calls, row.p50_ms) == (0, 1, None)


def test_calls_are_newest_first_and_page_back_with_before(store: UsageStore) -> None:
    events = [
        _event(
            ended_at=(NOW - timedelta(minutes=minutes)).isoformat(),
            started_at=(NOW - timedelta(minutes=minutes, seconds=1)).isoformat(),
        )
        for minutes in (3, 1, 2)
    ]
    for event in events:
        store.insert(event)

    first_page = store.calls({}, limit=2)
    second_page = store.calls({}, limit=2, before=str(first_page[-1].event["event_id"]))

    assert [call.event["event_id"] for call in first_page + second_page] == [
        events[1].event_id,
        events[2].event_id,
        events[0].event_id,
    ]


def test_calls_page_by_offset_and_count_what_the_filters_keep(
    store: UsageStore,
) -> None:
    events = [
        _event(
            ended_at=(NOW - timedelta(minutes=minutes)).isoformat(),
            started_at=(NOW - timedelta(minutes=minutes, seconds=1)).isoformat(),
        )
        for minutes in (1, 2, 3)
    ]
    for event in events:
        store.insert(event)

    second_page = store.calls({}, limit=2, offset=2)

    assert [call.event["event_id"] for call in second_page] == [events[2].event_id]
    assert store.count_calls({}) == 3
    assert store.count_calls({"model": ["nobody/model"]}) == 0


def test_calls_after_a_sequence_are_the_ones_stored_since(store: UsageStore) -> None:
    store.insert(_event())
    [seen] = store.calls({})
    later = _event(
        ended_at=(NOW - timedelta(hours=1)).isoformat(),
        started_at=(NOW - timedelta(hours=1, seconds=1)).isoformat(),
    )
    store.insert(later)

    assert [
        call.event["event_id"] for call in store.calls({}, after=seen.sequence)
    ] == [later.event_id]


def test_calls_filter_by_rollup_dimensions(store: UsageStore) -> None:
    store.insert(_event(attribution={"origin_id": "files"}))
    store.insert(_event(attribution={"origin_id": "routines"}))

    [call] = store.calls({"origin_id": ["routines"]})

    assert ModelCallEvent.model_validate(call.event).attribution.origin_id == "routines"
    with pytest.raises(UsageStoreError):
        store.calls({"harness": ["litellm"]})


def test_totals_group_by_cost_provenance(store: UsageStore) -> None:
    store.insert(_event())
    store.insert(_event(cost_usd="0.5", cost_provenance="REPORTED"))

    rows = {
        row.group["cost_provenance"]: row.calls
        for row in store.totals(["cost_provenance"], NOW - timedelta(hours=1))
    }

    assert rows == {"ESTIMATED": 1, "REPORTED": 1}


def _reading(
    used: float,
    minutes_ago: int,
    window: str = "five_hour",
    resets_in_minutes: int | None = None,
) -> LimitReading:
    return LimitReading(
        source="claude",
        window=window,
        used_percentage=used,
        resets_at=NOW + timedelta(minutes=resets_in_minutes)
        if resets_in_minutes is not None
        else None,
        window_seconds=18000 if window == "five_hour" else 604800,
        observed_at=NOW - timedelta(minutes=minutes_ago),
    )


def test_the_freshest_limit_reading_wins_whatever_order_it_arrives_in(
    store: UsageStore,
) -> None:
    store.record_limits([_reading(40, minutes_ago=5)])
    store.record_limits([_reading(10, minutes_ago=60)])

    [reading] = store.limits(NOW - timedelta(days=7))

    assert reading.used_percentage == 40


def test_a_stale_lower_share_of_the_same_window_does_not_replace_a_higher_one(
    store: UsageStore,
) -> None:
    # Two agents on one account: one saw 90%, an idle one keeps restamping its earlier 26%.
    store.record_limits([_reading(90, minutes_ago=4, resets_in_minutes=20)])
    store.record_limits([_reading(26, minutes_ago=0, resets_in_minutes=20)])

    [reading] = store.limits(NOW - timedelta(days=7))

    assert (reading.used_percentage, reading.observed_at) == (
        90,
        NOW - timedelta(minutes=4),
    )


def test_a_higher_share_of_the_same_window_wins_even_when_stamped_earlier(
    store: UsageStore,
) -> None:
    store.record_limits([_reading(26, minutes_ago=0, resets_in_minutes=20)])
    store.record_limits([_reading(90, minutes_ago=4, resets_in_minutes=20)])

    [reading] = store.limits(NOW - timedelta(days=7))

    assert reading.used_percentage == 90


def test_the_same_share_seen_again_moves_the_reading_forward(store: UsageStore) -> None:
    store.record_limits([_reading(90, minutes_ago=4, resets_in_minutes=20)])
    store.record_limits([_reading(90, minutes_ago=1, resets_in_minutes=20)])

    [reading] = store.limits(NOW - timedelta(days=7))

    assert reading.observed_at == NOW - timedelta(minutes=1)


def test_a_later_window_replaces_an_earlier_one_and_not_the_other_way(
    store: UsageStore,
) -> None:
    store.record_limits([_reading(90, minutes_ago=30, resets_in_minutes=-10)])
    store.record_limits([_reading(3, minutes_ago=1, resets_in_minutes=290)])
    store.record_limits([_reading(95, minutes_ago=0, resets_in_minutes=-10)])

    [reading] = store.limits(NOW - timedelta(days=7))

    assert reading.used_percentage == 3


def test_a_window_keeps_its_lowest_share_at_the_earliest_time_it_was_seen(
    store: UsageStore,
) -> None:
    store.record_limits([_reading(30, minutes_ago=40, resets_in_minutes=20)])
    store.record_limits([_reading(26, minutes_ago=50, resets_in_minutes=20)])
    # An idle agent restamping the old share later must not move where the window's rise began.
    store.record_limits([_reading(26, minutes_ago=0, resets_in_minutes=20)])
    store.record_limits([_reading(41, minutes_ago=2, resets_in_minutes=20)])

    [reading] = store.limits(NOW - timedelta(days=7))

    assert (
        reading.used_percentage,
        reading.first_used_percentage,
        reading.first_observed_at,
    ) == (
        41,
        26,
        NOW - timedelta(minutes=50),
    )


def test_a_later_window_starts_its_first_reading_over(store: UsageStore) -> None:
    store.record_limits([_reading(90, minutes_ago=30, resets_in_minutes=-10)])
    store.record_limits([_reading(3, minutes_ago=5, resets_in_minutes=290)])
    store.record_limits([_reading(7, minutes_ago=1, resets_in_minutes=290)])
    # A late reading of the earlier window changes nothing.
    store.record_limits([_reading(1, minutes_ago=0, resets_in_minutes=-10)])

    [reading] = store.limits(NOW - timedelta(days=7))

    assert (
        reading.used_percentage,
        reading.first_used_percentage,
        reading.first_observed_at,
    ) == (
        7,
        3,
        NOW - timedelta(minutes=5),
    )


def test_readings_of_one_window_in_one_batch_are_weighed_one_after_another(
    store: UsageStore,
) -> None:
    store.record_limits([_reading(90, minutes_ago=30, resets_in_minutes=-10)])
    store.record_limits(
        [
            _reading(3, minutes_ago=5, resets_in_minutes=290),
            _reading(5, minutes_ago=3, resets_in_minutes=290),
        ]
    )

    [reading] = store.limits(NOW - timedelta(days=7))

    assert (
        reading.used_percentage,
        reading.first_used_percentage,
        reading.first_observed_at,
    ) == (
        5,
        3,
        NOW - timedelta(minutes=5),
    )


def test_a_reading_dated_before_its_window_opened_is_never_its_first(
    store: UsageStore,
) -> None:
    # A writer catching up on old session logs stamps a day-old call with the reading it holds now.
    store.record_limits([_reading(6, minutes_ago=24 * 60, resets_in_minutes=180)])
    store.record_limits([_reading(9, minutes_ago=60, resets_in_minutes=180)])
    store.record_limits([_reading(12, minutes_ago=1, resets_in_minutes=180)])

    [reading] = store.limits(NOW - timedelta(days=7))

    assert (
        reading.used_percentage,
        reading.first_used_percentage,
        reading.first_observed_at,
    ) == (
        12,
        9,
        NOW - timedelta(minutes=60),
    )


def test_a_database_from_before_first_readings_gains_them(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE limit_window (source TEXT NOT NULL, window TEXT NOT NULL, label TEXT, "
            "used_percentage REAL, resets_at REAL, window_seconds INTEGER, observed_at REAL NOT NULL, "
            "PRIMARY KEY (source, window))"
        )
        connection.execute(
            "INSERT INTO limit_window VALUES ('claude', 'five_hour', NULL, 12, NULL, 18000, 0)"
        )

    reopened = UsageStore(path)
    try:
        reopened.record_limits([_reading(15, minutes_ago=1)])

        [reading] = reopened.limits(NOW - timedelta(days=7))
        assert (reading.used_percentage, reading.first_used_percentage) == (15, 15)
    finally:
        reopened.close()


def test_limits_come_shortest_window_first_and_old_ones_drop_out(
    store: UsageStore,
) -> None:
    store.record_limits(
        [_reading(8, minutes_ago=1, window="seven_day"), _reading(28, minutes_ago=1)]
    )

    assert [r.window for r in store.limits(NOW - timedelta(days=7))] == [
        "five_hour",
        "seven_day",
    ]
    assert store.limits(NOW) == []


def test_latency_filters_keep_calls_with_any_of_the_given_values(
    store: UsageStore,
) -> None:
    store.insert(_timed(1, billing_mode="API_KEY"))
    store.insert(_timed(3, billing_mode="UNKNOWN"))
    store.insert(_timed(9, billing_mode="SUBSCRIPTION"))

    [row] = store.latency(
        [], NOW - timedelta(hours=1), filters={"billing_mode": ["API_KEY", "UNKNOWN"]}
    )

    assert (row.timed_calls, row.p95_ms) == (2, 3000)
    with pytest.raises(UsageStoreError):
        store.latency([], NOW - timedelta(hours=1), filters={"nonsense": ["x"]})


def test_calls_filter_by_provider_and_any_of_several_values(store: UsageStore) -> None:
    store.insert(_event(billing_mode="API_KEY"))
    store.insert(_event(billing_mode="UNKNOWN"))
    store.insert(_event(billing_mode="SUBSCRIPTION"))
    store.insert(
        _event(provider="openai", model="openai/gpt-5.2", billing_mode="API_KEY")
    )

    calls = store.calls(
        {"provider": ["anthropic"], "billing_mode": ["API_KEY", "UNKNOWN"]}
    )

    assert sorted(str(call.event["billing_mode"]) for call in calls) == [
        "API_KEY",
        "UNKNOWN",
    ]


def test_totals_group_by_turn_and_calls_filter_by_it(store: UsageStore) -> None:
    store.insert(_event(attribution={"turn_id": "request-1", "session_id": "s-1"}))
    store.insert(_event(attribution={"turn_id": "request-1", "session_id": "s-1"}))
    store.insert(_event(attribution={"turn_id": "request-2", "session_id": "s-1"}))
    store.insert(_event())

    totals = {
        row.group["turn_id"]: row.calls
        for row in store.totals(["turn_id"], NOW - timedelta(hours=1))
    }
    [unattributed] = store.calls({"turn_id": [""]})

    assert totals == {None: 1, "request-1": 2, "request-2": 1}
    assert len(store.calls({"turn_id": ["request-1"]})) == 2
    assert store.count_calls({"session_id": ["s-1"], "turn_id": ["request-2"]}) == 1
    assert store.count_calls({"run_id": [""]}) == 4
    assert ModelCallEvent.model_validate(unattributed.event).attribution.turn_id is None


def test_a_database_from_before_turn_ids_gains_the_column_and_keeps_its_calls(
    tmp_path: Path,
) -> None:
    path = tmp_path / "old.db"
    old_store = UsageStore(path)
    old_store.insert(_event())
    old_store.close()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX model_call_turn_id")
        connection.execute("ALTER TABLE model_call DROP COLUMN turn_id")

    reopened = UsageStore(path)
    try:
        reopened.insert(_event(attribution={"turn_id": "request-1"}))
        totals = {
            row.group["turn_id"]: row.calls
            for row in reopened.totals(["turn_id"], NOW - timedelta(hours=1))
        }
        assert totals == {None: 1, "request-1": 1}
    finally:
        reopened.close()


def test_turns_group_calls_by_turn_and_a_call_with_no_turn_stands_alone(
    store: UsageStore,
) -> None:
    def at(minutes: int, **overrides: object) -> ModelCallEvent:
        return _event(
            ended_at=(NOW - timedelta(minutes=minutes)).isoformat(),
            started_at=(NOW - timedelta(minutes=minutes, seconds=2)).isoformat(),
            **overrides,
        )

    store.insert(
        at(
            5,
            attribution={"turn_id": "t-1", "origin_kind": "CHAT", "origin_id": "c-1"},
            billing_mode="SUBSCRIPTION",
        )
    )
    store.insert(
        at(
            3,
            attribution={"turn_id": "t-1", "origin_kind": "CHAT", "origin_id": "c-1"},
            billing_mode="SUBSCRIPTION",
            model="anthropic/claude-haiku-5-5",
            status="ERROR",
        )
    )
    loose = at(4, billing_mode="API_KEY", cost_usd="0.25", cost_provenance="REPORTED")
    store.insert(loose)

    turns = store.turns({}, limit=10)

    assert [(turn.turn_id, turn.event_id, turn.calls) for turn in turns] == [
        ("t-1", None, 2),
        (None, loose.event_id, 1),
    ]
    first = turns[0]
    assert first.ended_at == NOW - timedelta(minutes=3)
    assert first.started_at == NOW - timedelta(minutes=5, seconds=2)
    assert (first.failed_calls, first.billed_cost_usd, first.origin_id) == (
        1,
        None,
        "c-1",
    )
    assert first.subscription_cost_usd is not None and first.subscription_cost_usd > 0
    assert first.models == ["anthropic/claude-haiku-5-5", "anthropic/claude-opus-5-5"]
    assert first.tokens == {
        "input": 2000,
        "output": 200,
        "cache_read": 0,
        "cache_write": 0,
    }
    assert turns[1].billed_cost_usd == Decimal("0.25")
    assert store.count_turns({}) == 2


def test_turns_sum_only_the_calls_the_filters_keep_and_page_by_offset(
    store: UsageStore,
) -> None:
    for minutes, turn_id, mode in (
        (1, "t-1", "SUBSCRIPTION"),
        (2, "t-1", "API_KEY"),
        (3, "t-2", "SUBSCRIPTION"),
    ):
        store.insert(
            _event(
                ended_at=(NOW - timedelta(minutes=minutes)).isoformat(),
                started_at=(NOW - timedelta(minutes=minutes, seconds=1)).isoformat(),
                attribution={"turn_id": turn_id},
                billing_mode=mode,
            )
        )

    subscription = store.turns({"billing_mode": ["SUBSCRIPTION"]}, limit=1)
    second_page = store.turns({"billing_mode": ["SUBSCRIPTION"]}, limit=1, offset=1)

    assert [(turn.turn_id, turn.calls) for turn in subscription] == [("t-1", 1)]
    assert [turn.turn_id for turn in second_page] == ["t-2"]
    assert store.count_turns({"billing_mode": ["SUBSCRIPTION"]}) == 2
    assert len(store.calls({"turn_id": ["t-2"]})) == 1


def test_turns_in_a_window_keep_only_the_calls_that_ended_in_it(
    store: UsageStore,
) -> None:
    for minutes, turn_id in ((5, "t-1"), (30, "t-1"), (90, "t-2")):
        store.insert(
            _event(
                ended_at=(NOW - timedelta(minutes=minutes)).isoformat(),
                started_at=(NOW - timedelta(minutes=minutes, seconds=1)).isoformat(),
                attribution={"turn_id": turn_id},
            )
        )

    last_hour = store.turns({}, since=NOW - timedelta(hours=1))
    before_that = store.turns({}, until=NOW - timedelta(hours=1))

    assert [(turn.turn_id, turn.calls) for turn in last_hour] == [("t-1", 2)]
    assert [turn.turn_id for turn in before_that] == ["t-2"]
    assert store.count_turns({}, since=NOW - timedelta(minutes=10)) == 1
    assert store.count_turns({}, since=NOW - timedelta(hours=2)) == 2
    assert (
        store.count_turns(
            {}, since=NOW - timedelta(hours=2), until=NOW - timedelta(minutes=10)
        )
        == 2
    )

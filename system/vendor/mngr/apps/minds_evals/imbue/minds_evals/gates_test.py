"""The structural gates, exercised against the state a goal-driven conversation leaves behind.

The gate module runs in the verifier container and reads fixed absolute paths, so what is tested
here are the predicates behind the criteria, given the already-parsed state and case.
"""

from types import ModuleType
from typing import Any

import pytest

from imbue.minds_evals.data_types import CaseConfig
from imbue.minds_evals.data_types import EntryRecord
from imbue.minds_evals.data_types import GoalEntry
from imbue.minds_evals.data_types import TurnOutcome
from imbue.minds_evals.generate import oracle_entry_records


def _state(entries: list[dict[str, Any]], waits_done: int, test_state: str = "finished") -> dict[str, Any]:
    return {"test_state": test_state, "waits_done": waits_done, "entries": entries}


def _entry(index: int, kind: str, exchange_count: int, outcome: str) -> dict[str, Any]:
    return {"index": index, "kind": kind, "exchange_count": exchange_count, "outcome": outcome}


def _state_without_entries(
    waits_done: int, entry_count: int | None = None, test_state: str = "finished"
) -> dict[str, Any]:
    """A state.json from before per-entry records: the two ported counts and nothing else.

    `entry_count` rides the ported schema key, which is what such a rollout carries; omit it for a
    state that does not even have that.
    """
    state: dict[str, Any] = {"test_state": test_state, "waits_done": waits_done}
    if entry_count is not None:
        state["num_turns"] = entry_count
    return state


def test_all_turns_completed_accepts_a_goal_entry_that_sent_several_messages(gate_checks: ModuleType) -> None:
    entries = [_entry(0, "literal", 1, "completed"), _entry(1, "goal", 3, "satisfied")]

    assert gate_checks.is_every_entry_completed(
        _state(entries, waits_done=4), {"prompts": ["Build it", {"goal": "g"}]}
    )


def test_all_turns_completed_does_not_zero_a_budget_exhausted_entry(gate_checks: ModuleType) -> None:
    """An agent that cannot satisfy an unreasonable goal must not be conflated with a broken trial:
    the entry is recorded and shown to the outcome judge, not gated to zero."""
    entries = [_entry(0, "literal", 1, "completed"), _entry(1, "goal", 3, "budget_exhausted")]

    assert gate_checks.is_every_entry_completed(
        _state(entries, waits_done=4), {"prompts": ["Build it", {"goal": "g"}]}
    )


def test_all_turns_completed_rejects_a_conversation_that_never_reached_every_entry(gate_checks: ModuleType) -> None:
    entries = [_entry(0, "literal", 1, "completed")]

    assert not gate_checks.is_every_entry_completed(
        _state(entries, waits_done=1), {"prompts": ["Build it", {"goal": "g"}]}
    )


def test_all_turns_completed_rejects_a_string_entry_that_sent_no_message(gate_checks: ModuleType) -> None:
    """A string entry is exactly one message. Checking only the totals would let a goal entry's
    extra exchange pay for a deterministic turn the run dropped -- the shape the pre-entries rule
    (`waits_done == entry count`) made unrepresentable."""
    entries = [_entry(0, "literal", 1, "completed"), _entry(1, "literal", 0, "completed")]

    assert not gate_checks.is_every_entry_completed(
        _state(entries, waits_done=1), {"prompts": ["Build it", "And now?"]}
    )


def test_all_turns_completed_accepts_a_goal_entry_satisfied_without_sending_anything(
    gate_checks: ModuleType,
) -> None:
    """Only string entries carry the one-message floor: a client the first reply already satisfied
    is the most successful outcome an entry can have, not a dropped turn."""
    entries = [_entry(0, "literal", 1, "completed"), _entry(1, "goal", 0, "satisfied")]

    assert gate_checks.is_every_entry_completed(
        _state(entries, waits_done=1), {"prompts": ["Build it", {"goal": "g"}]}
    )


def test_all_turns_completed_rejects_entries_that_do_not_account_for_the_messages_sent(
    gate_checks: ModuleType,
) -> None:
    """The entry records and the message count are two views of the same conversation; a trial whose
    views disagree is not a gradeable record."""
    entries = [_entry(0, "literal", 1, "completed"), _entry(1, "goal", 3, "satisfied")]

    assert not gate_checks.is_every_entry_completed(
        _state(entries, waits_done=2), {"prompts": ["Build it", {"goal": "g"}]}
    )


@pytest.mark.parametrize(
    "state",
    [
        None,
        {"test_state": "finished", "waits_done": 1, "entries": "nope"},
        # A present-but-null `entries` is a defect in the run, not a rollout from before the key
        # existed: it must be rejected even though the two ported counts would satisfy the old rule.
        {**_state_without_entries(waits_done=1, entry_count=1), "entries": None},
        {"test_state": "timed_out", "waits_done": 1, "entries": [_entry(0, "literal", 1, "completed")]},
        {"test_state": "finished", "waits_done": 1, "entries": [_entry(0, "literal", 1, "abandoned")]},
        {
            "test_state": "finished",
            "waits_done": 1,
            "entries": [{"index": 0, "kind": "literal", "exchange_count": "1", "outcome": "completed"}],
        },
        {"test_state": "finished", "waits_done": True, "entries": [_entry(0, "literal", 1, "completed")]},
    ],
)
def test_all_turns_completed_rejects_state_it_cannot_read_as_a_finished_conversation(
    gate_checks: ModuleType,
    state: dict[str, Any] | None,
) -> None:
    """The gate runs in the verifier container, where raising is not a verdict: anything it cannot
    read as a finished conversation -- including a count that is not a count -- has to come back
    False."""
    assert not gate_checks.is_every_entry_completed(state, {"prompts": ["Build it"]})


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (_state_without_entries(waits_done=2, entry_count=2), True),
        # The conversation stopped short, which is what the old rule existed to catch.
        (_state_without_entries(waits_done=1, entry_count=2), False),
        (_state_without_entries(waits_done=2, entry_count=2, test_state="timed_out"), False),
        (_state_without_entries(waits_done=2), False),
    ],
)
def test_all_turns_completed_reads_a_rollout_captured_before_per_entry_records(
    gate_checks: ModuleType,
    state: dict[str, Any],
    expected: bool,
) -> None:
    """A state.json with no `entries` at all is a rollout from before this schema, not a broken run.
    Regrading one against a regenerated dataset must not score it zero for a missing key -- and a
    dataset with no goal entries sends one message per entry, so the old rule still decides it."""
    assert gate_checks.is_every_entry_completed(state, {"prompts": ["Build it", "And now?"]}) is expected


def _goal_case_config() -> CaseConfig:
    return CaseConfig(
        case_id="goal-case",
        persona="Non-technical founder.",
        prompts=("Build me a to-do app", GoalEntry(goal="See it running", max_exchanges=4)),
        timeout_seconds=1800.0,
        verification_timeout_seconds=600.0,
        mngr_branch="main",
        mngr_sha="a" * 40,
        dwt_repo="https://example.invalid/dwt.git",
        dwt_branch="main",
        dwt_sha="c" * 40,
        avg_word_count_baseline=100.0,
        expectations=None,
        authored_expectations=None,
    )


def test_the_oracles_fabricated_state_passes_the_re_founded_gates(gate_checks: ModuleType) -> None:
    """The oracle sends one literal message per entry, including goal entries. Its state must
    reconcile with that, or `harbor run -a oracle` scores 0 on every dataset with a goal entry."""
    case_config = _goal_case_config()
    entries = oracle_entry_records(case_config)

    assert gate_checks.is_every_entry_completed(
        _state(entries, waits_done=len(case_config.prompts)),
        # The dict `write_task_dir` writes to tests/case.json rather than a stand-in for it: the
        # gate counts the entries and dispatches its per-entry floor on that serialization, so it
        # is the thing the oracle's records have to reconcile against.
        case_config.model_dump(mode="json"),
    )


def test_agent_engaged_substantively_keys_off_the_messages_sent_not_the_entries(gate_checks: ModuleType) -> None:
    """One goal entry can send several messages and can also be satisfied without sending any, so
    the distinctness bar follows the messages that were actually sent."""
    # Four replies to three messages, three of them distinct: engaged.
    assert gate_checks.is_agent_engaged_substantively(["Building it.", "Nearly.", "Here.", "Here."], 3)
    # A conversation of one message can only be asked for one distinct reply.
    assert gate_checks.is_agent_engaged_substantively(["Building it."], 1)
    # The same line every time is the wedged agent this gate exists to catch -- including when only
    # one of the several messages sent drew a non-empty reply at all.
    assert not gate_checks.is_agent_engaged_substantively(["Building it.", "Building it.", "Building it."], 3)
    assert not gate_checks.is_agent_engaged_substantively(["Building it."], 3)
    assert not gate_checks.is_agent_engaged_substantively(["Not logged in - Please run /login"], 1)
    assert not gate_checks.is_agent_engaged_substantively([], 1)
    # A run that recorded no message cannot have shown engagement.
    assert not gate_checks.is_agent_engaged_substantively(["Building it."], 0)


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (None, None),
        ({"waits_done": 3}, 3),
        ({"waits_done": "3"}, None),
        ({"waits_done": True}, None),
        ({}, None),
    ],
)
def test_messages_sent_reads_only_a_real_count(
    gate_checks: ModuleType,
    state: dict[str, Any] | None,
    expected: int | None,
) -> None:
    """The engagement bar is founded on this number, so anything it cannot read as a count has to
    come back as 'unknown' rather than as zero, which would lower the bar instead of failing."""
    assert gate_checks.messages_sent(state) == expected


def test_the_gate_modules_outcome_names_are_exactly_the_drivers(gate_checks: ModuleType) -> None:
    """The gate runs in the verifier container and cannot import the driver, so it restates the
    outcome names. A name added on one side of that boundary and not the other would silently fail
    every trial that recorded it."""
    assert gate_checks._ENTRY_OUTCOMES == {outcome.value for outcome in TurnOutcome}


def test_the_oracles_fabricated_entry_records_are_real_entry_records() -> None:
    """The oracle writes its state.json by hand, on the far side of the same boundary; the records
    it fabricates have to be the ones the driver would have written."""
    case_config = _goal_case_config()

    raw_records = oracle_entry_records(case_config)
    records = [EntryRecord(**record) for record in raw_records]

    assert [record.index for record in records] == list(range(len(case_config.prompts)))
    assert records[-1].outcome == TurnOutcome.SATISFIED
    # Every key, not merely enough of them to parse: a record short of one is shaped unlike the ones
    # a real trial writes, in exactly the run those trials are compared against.
    assert all(set(record) == set(EntryRecord.model_fields) for record in raw_records)

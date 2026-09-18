"""What the check-time facts read out of a step directory, and how they tell the three outcomes apart.

Each test writes a healthy step and then edits one record, because that is the only way a job
directory ever differs: a record is missing, unreadable, or says something other than the healthy
thing. The block is asserted through `compute_trial_facts`, which is what the checker calls.
"""

import inspect
import json
import subprocess
import tarfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from typing import Final

import pytest

from imbue.imbue_common.model_update import to_update
from imbue.minds_evals import evidence_collection
from imbue.minds_evals import fact_sources
from imbue.minds_evals import ui_flows
from imbue.minds_evals.check_diagnostics import StepFacts
from imbue.minds_evals.check_diagnostics import TrialFacts
from imbue.minds_evals.check_diagnostics import compute_trial_facts
from imbue.minds_evals.data_types import NOT_RECORDED
from imbue.minds_evals.driver import DriverEventType
from imbue.minds_evals.driver import MindsPersonaDriver
from imbue.minds_evals.evidence_facts import DIGEST_MARKERS_READ
from imbue.minds_evals.evidence_facts import FEED_EVENT_RECORD_TYPE
from imbue.minds_evals.evidence_facts import STATE_KEYS_READ
from imbue.minds_evals.evidence_facts import feed_goal_span
from imbue.minds_evals.evidence_facts import flow_facts
from imbue.minds_evals.evidence_facts import is_every_call_answered_once
from imbue.minds_evals.evidence_facts import read_page_state
from imbue.minds_evals.template_loading import load_template_module
from imbue.minds_evals.testing import DIAGNOSTIC_CLIENT_MESSAGE
from imbue.minds_evals.testing import DIAGNOSTIC_STEP_NAMES
from imbue.minds_evals.testing import DIAGNOSTIC_WORKER_NAME
from imbue.minds_evals.testing import DiagnosticStepFiles
from imbue.minds_evals.testing import diagnostic_manifest
from imbue.minds_evals.testing import diagnostic_state
from imbue.minds_evals.testing import diagnostic_step
from imbue.minds_evals.testing import diagnostic_trajectory
from imbue.minds_evals.testing import edited_diagnostic_step
from imbue.minds_evals.testing import write_diagnostic_trial_dir
from imbue.minds_evals.testing import write_workspace_snapshot

_MINDS_APP_EXPECTATIONS: Mapping[str, Any] = {
    "outcome": "The workspace serves the seeded fixture.",
    "deliverable": {
        "kind": "minds-app",
        "min_registered_apps": 0,
        "http": [{"target": "todo-fixture", "expect_status": 200}],
        "files": [{"glob": "workspace/index.html", "min_count": 1}],
    },
    "test_commands": ["test -s index.html"],
    "ui_flows": [{"name": "plain", "actions": "click Add", "expect": "'walk dog' is listed"}],
}


_PROCESS_AND_TIMING_EXPECTATIONS: Mapping[str, Any] = {
    "outcome": "The workspace serves the seeded fixture.",
    "process": {"required_skills": ["build-app"], "max_worker_launches": 0},
    "timing": {"fast_seconds": 150, "slow_seconds": 600},
}


def _flat_trial_facts(tmp_path: Path, step: DiagnosticStepFiles) -> StepFacts:
    """The one block of a flat trial whose single step is the one given."""
    trial_dir = write_diagnostic_trial_dir(tmp_path / "job", "behaviour__aaaaaaa", [step])
    (facts_step,) = compute_trial_facts(trial_dir, tmp_path / "work").steps
    return facts_step


def _stepped_trial_facts(tmp_path: Path, steps: list[DiagnosticStepFiles]) -> TrialFacts:
    trial_dir = write_diagnostic_trial_dir(tmp_path / "job", "behaviour__aaaaaaa", steps)
    return compute_trial_facts(trial_dir, tmp_path / "work")


# --- the three outcomes ---


def test_a_record_that_was_never_due_leaves_its_facts_out_of_the_block(tmp_path: Path) -> None:
    """A case that commissions nothing declares no HTTP check, no bundle and no flow, so there is
    nothing to say about them -- which is a different claim from the instrument not writing them."""
    facts = _flat_trial_facts(tmp_path, diagnostic_step())

    assert not [name for name in facts.values if name.startswith(("http.", "files.", "bundle.", "flow.", "repo."))]
    # The trial pulled no snapshot, so both snapshot facts are absent rather than null.
    assert "snapshot.bytes" not in facts.values and "snapshot.readable" not in facts.values
    assert facts.not_recorded_names == []


def test_a_record_that_was_due_and_is_missing_is_not_recorded(tmp_path: Path) -> None:
    """The evidence manifest is due on every step that collected. Without it nothing can be said
    about the entries, and `manifest.readable` says which of the two happened."""
    step = edited_diagnostic_step(diagnostic_step(), {"agent/verification/manifest.json": None})

    facts = _flat_trial_facts(tmp_path, step)

    assert facts.values["manifest.readable"] is False
    assert facts.values["evidence.errored_entries"] is NOT_RECORDED
    assert facts.values["evidence.statuses"] is NOT_RECORDED
    assert "evidence.errored_entries" in facts.not_recorded_names


def test_a_record_that_is_there_and_cannot_be_read_is_not_recorded_either(tmp_path: Path) -> None:
    step = edited_diagnostic_step(diagnostic_step(), {"agent/verification/manifest.json": "{ this is not json"})

    facts = _flat_trial_facts(tmp_path, step)

    assert facts.values["manifest.readable"] is False
    assert facts.values["evidence.statuses"] is NOT_RECORDED


def test_a_record_that_is_there_and_cannot_answer_is_null(tmp_path: Path) -> None:
    """A registry the collector could not read leaves the delivered set unknown, which is not the
    same claim as a workspace that delivered nothing."""
    step = edited_diagnostic_step(
        diagnostic_step(),
        {"agent/verification/manifest.json": json.dumps({**diagnostic_manifest(), "is_registry_present": False})},
    )

    facts = _flat_trial_facts(tmp_path, step)

    assert facts.values["apps.delivered"] is None
    assert facts.values["apps.registered_all_running"] is None
    assert "apps.delivered" not in facts.not_recorded_names


def test_a_failed_ticket_capture_reads_as_null_and_a_missing_one_as_not_recorded(tmp_path: Path) -> None:
    """A capture that failed writes its reason, so "could not read the directory" and "the workspace
    holds no tickets" stay apart on disk."""
    failed = edited_diagnostic_step(
        diagnostic_step(),
        {"agent/verification/tickets.jsonl": evidence_collection.ticket_failure_jsonl("bridge_failed")},
    )
    missing = edited_diagnostic_step(diagnostic_step(), {"agent/verification/tickets.jsonl": None})

    failed_facts = _flat_trial_facts(tmp_path / "failed", failed)
    missing_facts = _flat_trial_facts(tmp_path / "missing", missing)

    assert failed_facts.values["tickets.step_titles"] is None
    assert missing_facts.values["tickets.step_titles"] is NOT_RECORDED


def test_a_declared_check_with_no_manifest_entry_is_not_recorded(tmp_path: Path) -> None:
    """The case declared the probe, so an entry for it was due; a manifest that carries none means
    the collector did not record what it found."""
    step = diagnostic_step(authored_expectations=_MINDS_APP_EXPECTATIONS)

    facts = _flat_trial_facts(tmp_path, step)

    assert facts.values["http.http_1_todo_fixture.status"] is NOT_RECORDED
    assert facts.values["files.files_0.matched"] is NOT_RECORDED
    assert facts.values["flow.plain.status"] is NOT_RECORDED


def test_a_declared_check_reads_its_structured_answer_off_the_manifest_entry(tmp_path: Path) -> None:
    entries = [
        {
            "entry_id": "http_1_todo_fixture",
            "check_class": "http",
            "status": "passed",
            "env": "live",
            "reason": "",
            "detail": "",
            "evidence_path": "",
            "status_code": 200,
        },
        {
            "entry_id": "files_0",
            "check_class": "files",
            "status": "failed",
            "env": "live",
            "reason": "too_few_matches",
            "detail": "",
            "evidence_path": "",
            "matched_count": 0,
        },
        {
            "entry_id": "test_command_0",
            "check_class": "test_command",
            "status": "passed",
            "env": "live",
            "reason": "",
            "detail": "",
            "evidence_path": "",
            "exit_code": 0,
        },
    ]
    step = edited_diagnostic_step(
        diagnostic_step(authored_expectations=_MINDS_APP_EXPECTATIONS),
        {"agent/verification/manifest.json": json.dumps(diagnostic_manifest(entries=entries))},
    )

    facts = _flat_trial_facts(tmp_path, step)

    assert facts.values["http.http_1_todo_fixture.status"] == 200
    assert facts.values["files.files_0.matched"] == 0
    assert facts.values["test_commands.exit_codes"] == [0]
    assert facts.values["evidence.statuses"] == {
        "http_1_todo_fixture": "passed",
        "files_0": "failed",
        "test_command_0": "passed",
    }
    assert facts.values["evidence.errored_entries"] == []


def test_the_manifests_process_and_timing_entries_reach_the_entry_statuses(tmp_path: Path) -> None:
    """`evidence.statuses` speaks for every class the collector records, not a list kept in step with
    it: a class added to the manifest reaches the facts without the checker being taught its name."""
    entries = [
        {
            "entry_id": "skill_required_build_app",
            "check_class": "process",
            "status": "passed",
            "env": "live",
            "reason": "",
            "detail": "",
            "evidence_path": "",
        },
        {
            "entry_id": "worker_launches",
            "check_class": "process",
            "status": "failed",
            "env": "live",
            "reason": "worker_launches_exceeded",
            "detail": "",
            "evidence_path": "",
        },
        {
            "entry_id": "time_to_goal",
            "check_class": "timing",
            "status": "passed",
            "env": "live",
            "reason": "",
            "detail": "",
            "evidence_path": "",
            "value": 121.1,
        },
    ]
    step = edited_diagnostic_step(
        diagnostic_step(authored_expectations=_PROCESS_AND_TIMING_EXPECTATIONS),
        {"agent/verification/manifest.json": json.dumps(diagnostic_manifest(entries=entries))},
    )

    facts = _flat_trial_facts(tmp_path, step)

    assert facts.values["case.readable"] is True
    assert facts.values["manifest.readable"] is True
    assert facts.values["evidence.statuses"] == {
        "skill_required_build_app": "passed",
        "worker_launches": "failed",
        "time_to_goal": "passed",
    }
    assert facts.values["evidence.errored_entries"] == []


# --- the trajectory, the steps and the spend ---


def test_the_boundary_markers_name_the_steps_harbor_ran_in_order(tmp_path: Path) -> None:
    facts = _stepped_trial_facts(
        tmp_path,
        [
            diagnostic_step(DIAGNOSTIC_STEP_NAMES[0], step_index=0, step_total=2),
            diagnostic_step(DIAGNOSTIC_STEP_NAMES[1], step_index=1, step_total=2),
        ],
    )

    assert facts.steps[-1].values["steps.boundary_markers"] == ["work", "check"]
    assert facts.steps[-1].values["steps.boundary_markers_match_case"] is True
    assert facts.steps[-1].values["steps.boundary_markers_followed_by_opening"] is True
    assert facts.steps[-1].values["steps.trajectory_cumulative"] is True
    assert facts.steps[-1].values["steps.entries_before"] == 1


def test_a_marker_the_document_dropped_fails_the_agreement_with_the_case(tmp_path: Path) -> None:
    """A marker is placed by exact message or by timestamp and dropped when neither matches, so a
    trajectory naming fewer steps than harbor ran is the reading worth catching."""
    second = diagnostic_step(DIAGNOSTIC_STEP_NAMES[1], step_index=1, step_total=2)
    second = edited_diagnostic_step(
        second, {"agent/trajectory.json": json.dumps(diagnostic_trajectory(DIAGNOSTIC_STEP_NAMES[:1]))}
    )

    facts = _stepped_trial_facts(
        tmp_path, [diagnostic_step(DIAGNOSTIC_STEP_NAMES[0], step_index=0, step_total=2), second]
    )

    assert facts.steps[-1].values["steps.boundary_markers"] == ["work"]
    assert facts.steps[-1].values["steps.boundary_markers_match_case"] is False


def test_a_flat_trial_marks_no_steps_and_still_agrees_with_its_case(tmp_path: Path) -> None:
    facts = _flat_trial_facts(tmp_path, diagnostic_step())

    assert facts.values["steps.boundary_markers"] == []
    assert facts.values["steps.boundary_markers_match_case"] is True
    # There is no earlier step for the log to be local to, or for the spend deltas to accumulate over.
    assert "steps.driver_log_step_local" not in facts.values
    assert "steps.spend_deltas_sum" not in facts.values


def test_the_spend_deltas_have_to_add_up_to_the_running_total(tmp_path: Path) -> None:
    """The driver publishes each step's share rather than the trial's standing, so a step that
    republished the whole total would double-count the trial's spend."""
    second = diagnostic_step(DIAGNOSTIC_STEP_NAMES[1], step_index=1, step_total=2)

    healthy = _stepped_trial_facts(
        tmp_path / "healthy", [diagnostic_step(DIAGNOSTIC_STEP_NAMES[0], step_index=0, step_total=2), second]
    )
    republished = _stepped_trial_facts(
        tmp_path / "republished",
        [
            diagnostic_step(DIAGNOSTIC_STEP_NAMES[0], step_index=0, step_total=2),
            second.model_copy_update(to_update(second.field_ref().input_token_delta, 2_000)),
        ],
    )

    assert healthy.steps[-1].values["steps.spend_deltas_sum"] is True
    assert republished.steps[-1].values["steps.spend_deltas_sum"] is False


def test_a_step_local_driver_log_starts_no_earlier_than_the_step_before_it_ended(tmp_path: Path) -> None:
    first = diagnostic_step(DIAGNOSTIC_STEP_NAMES[0], step_index=0, step_total=2)
    second = diagnostic_step(DIAGNOSTIC_STEP_NAMES[1], step_index=1, step_total=2)
    cumulative = edited_diagnostic_step(
        second, {"agent/driver.log": first.files["agent/driver.log"] + second.files["agent/driver.log"]}
    )

    assert (
        _stepped_trial_facts(tmp_path / "local", [first, second]).steps[-1].values["steps.driver_log_step_local"]
        is True
    )
    assert (
        _stepped_trial_facts(tmp_path / "cumulative", [first, cumulative])
        .steps[-1]
        .values["steps.driver_log_step_local"]
        is False
    )


def test_the_transcript_facts_are_omitted_before_the_conversation_began(tmp_path: Path) -> None:
    """A trial that gave up in preparation publishes no document, so there is nothing about a
    transcript to record -- and nothing the instrument failed to write."""
    step = edited_diagnostic_step(
        diagnostic_step(),
        {
            "agent/state.json": json.dumps(diagnostic_state(preparation_stage="created")),
            "agent/trajectory.json": None,
        },
    )

    facts = _flat_trial_facts(tmp_path, step)

    assert "transcript.source" not in facts.values
    assert "tools.every_call_has_one_result" not in facts.values


def test_the_transcript_facts_are_not_recorded_when_the_conversation_ran_without_one(tmp_path: Path) -> None:
    step = edited_diagnostic_step(diagnostic_step(), {"agent/trajectory.json": None})

    facts = _flat_trial_facts(tmp_path, step)

    assert facts.values["transcript.source"] is NOT_RECORDED
    assert facts.values["tools.every_call_has_one_result"] is NOT_RECORDED


@pytest.mark.parametrize(
    ("steps", "is_answered"),
    [
        pytest.param(
            [
                {
                    "source": "agent",
                    "tool_calls": [{"tool_call_id": "call-1"}],
                    "observation": {"results": [{"source_call_id": "call-1"}]},
                }
            ],
            True,
            id="one call, one result",
        ),
        pytest.param(
            [{"source": "agent", "tool_calls": [{"tool_call_id": "call-1"}]}], False, id="call with no result"
        ),
        pytest.param(
            [{"source": "agent", "observation": {"results": [{"source_call_id": "call-1"}]}}],
            False,
            id="result with no call",
        ),
        pytest.param(
            [
                {
                    "source": "agent",
                    "tool_calls": [{"tool_call_id": "call-1"}],
                    "observation": {"results": [{"source_call_id": "call-1"}, {"source_call_id": "call-1"}]},
                }
            ],
            False,
            id="call answered twice",
        ),
    ],
)
def test_every_call_is_answered_once_in_both_directions(steps: list[dict[str, Any]], is_answered: bool) -> None:
    assert is_every_call_answered_once(steps) is is_answered


# --- the time to the satisfied goal ---

_FIRST_SEND_AT: Final[str] = "2026-09-01T00:10:00+00:00"
_REPLY_SEEN_AT: Final[str] = "2026-09-01T00:10:12+00:00"
# What the workspace's own feed puts on the same exchange. Neither end matches the driver's stamp:
# the driver anchors once its send round trip has come back, and closes once a poll has seen the
# agent idle again, which is well after the reply the feed stamps.
_FEED_SEND_AT: Final[str] = "2026-09-01T00:10:00.400Z"
_FEED_REPLY_AT: Final[str] = "2026-09-01T00:10:07.900Z"


def _timed_state(**overrides: Any) -> dict[str, Any]:
    """A state whose goal-holding client was satisfied by the reply to the first client message."""
    recorded: dict[str, Any] = {
        "entries": [
            {
                "index": 0,
                "kind": "goal",
                "exchange_count": 1,
                "outcome": "satisfied",
                "detail": "the agent did what was asked",
                "satisfied_at": _REPLY_SEEN_AT,
                "satisfied_at_turn": 1,
            }
        ],
        "turns": [
            {
                "index": 1,
                "entry_index": 0,
                "exchange": 0,
                "sent_at": _FIRST_SEND_AT,
                "replied_at": _REPLY_SEEN_AT,
                "reply_seconds": 12.0,
                "agent_message_count": 1,
                "message_count": 1,
                "tokens": {"input": 10, "output": 20, "cache_read": 0, "cache_write": 0},
                "cost_usd": 0.01,
            }
        ],
        "elapsed_seconds": 900.0,
    }
    return diagnostic_state(**{**recorded, **overrides})


def _diagnostic_feed(send_at: str = _FEED_SEND_AT, reply_at: str = _FEED_REPLY_AT) -> str:
    """The feed the driver polled, as one exchange: the client message it sent, then the reply."""
    records = [
        {
            "type": "feed_event",
            "event": {"type": "user_message", "timestamp": send_at, "content": DIAGNOSTIC_CLIENT_MESSAGE},
        },
        {"type": "feed_event", "event": {"type": "assistant_message", "timestamp": reply_at, "text": "done"}},
        {"type": "decider_message", "text": "", "entry_index": 0},
    ]
    return "".join("{}\n".format(json.dumps(record)) for record in records)


def _timed_step(state: Mapping[str, Any] | None = None, feed: str | None = None) -> DiagnosticStepFiles:
    """A healthy step of a case that declares a timing block, with its conversation records and the
    feed that independently timed the same exchange."""
    return edited_diagnostic_step(
        diagnostic_step(authored_expectations=_PROCESS_AND_TIMING_EXPECTATIONS),
        {
            "agent/state.json": json.dumps(dict(state) if state is not None else _timed_state()),
            "agent/driver_events.jsonl": _diagnostic_feed() if feed is None else feed,
        },
    )


def test_the_timing_facts_name_the_turn_the_goal_was_met_on_and_hold_it_against_the_feed(tmp_path: Path) -> None:
    """The driver and the feed record one exchange from two sides, so the fact that matters is that
    the measured span opens and closes on it -- a span anchored on the welcome turn would not."""
    facts = _flat_trial_facts(tmp_path, _timed_step())

    assert facts.values["timing.turn_index"] == 1
    assert facts.values["timing.seconds_within_elapsed"] is True
    assert facts.values["timing.agrees_with_feed"] is True


def test_a_case_that_declares_no_timing_block_records_none_of_it(tmp_path: Path) -> None:
    facts = _flat_trial_facts(tmp_path, diagnostic_step())

    assert not [name for name in facts.values if name.startswith("timing.")]


def test_a_state_that_recorded_no_turns_cannot_time_the_goal(tmp_path: Path) -> None:
    """The per-turn records are what say when the conversation started, so a state without them is
    the instrument failing to write a record rather than a trial that was never timed."""
    state = {key: value for key, value in _timed_state().items() if key != "turns"}

    facts = _flat_trial_facts(tmp_path, _timed_step(state))

    assert facts.values["timing.turn_index"] is NOT_RECORDED
    assert "timing.agrees_with_feed" in facts.not_recorded_names


def test_a_goal_no_client_was_ever_satisfied_by_leaves_the_span_null(tmp_path: Path) -> None:
    """An unsatisfied client is a measurement of the agent, not a record the instrument lost."""
    entry = {"index": 0, "kind": "goal", "exchange_count": 1, "outcome": "budget_exhausted", "detail": "out of budget"}

    facts = _flat_trial_facts(tmp_path, _timed_step(_timed_state(entries=[entry])))

    assert facts.values["timing.turn_index"] is None
    assert facts.values["timing.agrees_with_feed"] is None


def test_a_span_longer_than_the_trial_itself_is_a_miss(tmp_path: Path) -> None:
    """The measured span sits inside the trial that produced it, so one that does not is a span
    anchored on something other than the conversation."""
    facts = _flat_trial_facts(tmp_path, _timed_step(_timed_state(elapsed_seconds=5.0)))

    assert facts.values["timing.seconds_within_elapsed"] is False


def test_a_span_that_opens_on_another_exchange_disagrees_with_the_feed(tmp_path: Path) -> None:
    """Minutes apart is the disagreement worth catching: a span anchored on the welcome turn rather
    than on the case's first client message reads exactly like this."""
    feed = _diagnostic_feed(send_at="2026-09-01T00:02:00Z")

    facts = _flat_trial_facts(tmp_path, _timed_step(feed=feed))

    assert facts.values["timing.agrees_with_feed"] is False


def test_a_span_that_closes_before_the_reply_the_feed_shows_disagrees(tmp_path: Path) -> None:
    """The driver closes once a poll has seen the agent idle again, so its close is never earlier
    than the reply the feed stamps: one that is closed on some other turn's."""
    feed = _diagnostic_feed(reply_at="2026-09-01T00:12:00Z")

    facts = _flat_trial_facts(tmp_path, _timed_step(feed=feed))

    assert facts.values["timing.agrees_with_feed"] is False


def test_a_feed_missing_an_end_of_the_span_cannot_answer(tmp_path: Path) -> None:
    """The feed a stepped case's later step carries holds no earlier step's messages, so the
    agreement is unanswerable there rather than false."""
    feed = (
        json.dumps(
            {
                "type": "feed_event",
                "event": {"type": "user_message", "timestamp": _FEED_SEND_AT, "content": DIAGNOSTIC_CLIENT_MESSAGE},
            }
        )
        + "\n"
    )

    facts = _flat_trial_facts(tmp_path, _timed_step(feed=feed))

    assert facts.values["timing.turn_index"] == 1
    assert facts.values["timing.agrees_with_feed"] is None


def test_the_feed_record_kind_the_facts_read_is_the_one_the_driver_writes() -> None:
    """Spelled in both places rather than shared, for the reason the state keys are: a renamed kind
    must be caught here instead of reading as a trial whose feed recorded nothing."""
    assert FEED_EVENT_RECORD_TYPE == DriverEventType.FEED_EVENT.value


def test_the_feed_span_reads_the_atif_shaped_vintage_of_the_stream_too() -> None:
    """mngr's own emitters write ATIF-shaped `step` records where the chat app writes
    `user_message` and `assistant_message`, and a trial's feed carries whichever its harness emitted."""
    events = [
        {"type": "step", "source": "user", "message": "go", "timestamp": "2026-09-01T00:00:00+00:00"},
        {"type": "step", "source": "agent", "message": "done", "timestamp": "2026-09-01T00:00:30+00:00"},
    ]

    span = feed_goal_span(events, ["go"], 1)

    assert span is not None and [moment.isoformat() for moment in span] == [
        "2026-09-01T00:00:00+00:00",
        "2026-09-01T00:00:30+00:00",
    ]


# --- workers ---


def test_the_worker_lists_are_compared_only_on_a_complete_listing(tmp_path: Path) -> None:
    """`mngr list` answers with the agents it reached and says what it could not, so an agent's
    absence means nothing until the listing says it saw everything."""
    listed = diagnostic_step(listed_worker_names=[DIAGNOSTIC_WORKER_NAME])
    incomplete = edited_diagnostic_step(
        listed,
        {
            "agent/verification/workers/listing.json": json.dumps(
                {"exit_code": 1, "errors": ["modal unreachable"], "is_complete": False}
            )
        },
    )

    complete_facts = _flat_trial_facts(tmp_path / "complete", listed)
    incomplete_facts = _flat_trial_facts(tmp_path / "incomplete", incomplete)

    assert complete_facts.values["listing.complete"] is True
    assert complete_facts.values["workers.no_phantoms"] is True
    assert incomplete_facts.values["listing.complete"] is False
    assert [
        incomplete_facts.values[name]
        for name in (
            "workers.no_phantoms",
            "workers.discovered_equals_listed",
            "workers.captured_equals_listed",
            "workers.embedded_equals_listed",
        )
    ] == [None, None, None, None]


def test_the_embedded_workers_are_compared_against_the_listing_too(tmp_path: Path) -> None:
    """A worker the listing names and the trajectory does not embed is delegated work the transcript
    account never saw."""
    listed = diagnostic_step(listed_worker_names=[DIAGNOSTIC_WORKER_NAME])

    unembedded = _flat_trial_facts(tmp_path / "unembedded", listed)
    embedded = _flat_trial_facts(
        tmp_path / "embedded",
        edited_diagnostic_step(
            listed, {"agent/trajectory.json": json.dumps(_trajectory_embedding(DIAGNOSTIC_WORKER_NAME))}
        ),
    )

    assert unembedded.values["workers.embedded"] == []
    assert unembedded.values["workers.embedded_equals_listed"] is False
    assert embedded.values["workers.embedded"] == [DIAGNOSTIC_WORKER_NAME]
    assert embedded.values["workers.embedded_equals_listed"] is True


def test_a_worker_running_another_harness_than_the_lead_is_a_disagreement(tmp_path: Path) -> None:
    """A claude worker runs the template's pinned harness whatever the lead was signed in on, and the
    two records spell a compound harness differently, so only a different harness is a miss."""
    listed = diagnostic_step(listed_worker_names=[DIAGNOSTIC_WORKER_NAME], harness="pi-coding")
    spelled = edited_diagnostic_step(
        listed, {"agent/verification/workers/agents.json": _listing_json(DIAGNOSTIC_WORKER_NAME, "pi_coding")}
    )
    other = edited_diagnostic_step(
        listed, {"agent/verification/workers/agents.json": _listing_json(DIAGNOSTIC_WORKER_NAME, "claude")}
    )

    assert _flat_trial_facts(tmp_path / "same", listed).values["workers.harness_is_lead_harness"] is True
    assert _flat_trial_facts(tmp_path / "spelled", spelled).values["workers.harness_is_lead_harness"] is True
    assert _flat_trial_facts(tmp_path / "other", other).values["workers.harness_is_lead_harness"] is False


def test_a_discovered_worker_the_listing_never_names_is_a_phantom(tmp_path: Path) -> None:
    """A launch command that failed is still counted as a worker by the command-text discovery, so a
    trial can record a worker that never existed."""
    step = diagnostic_step(listed_worker_names=[DIAGNOSTIC_WORKER_NAME])
    step = edited_diagnostic_step(
        step, {"agent/verification/workers/agents.json": json.dumps({"agents": [], "errors": []})}
    )

    facts = _flat_trial_facts(tmp_path, step)

    assert facts.values["workers.no_phantoms"] is False
    assert facts.values["workers.discovered"] == [DIAGNOSTIC_WORKER_NAME]
    assert facts.values["workers.listed_agent_created"] == []


def test_a_launch_the_caps_left_uncaptured_is_discovered_and_not_captured(tmp_path: Path) -> None:
    step = edited_diagnostic_step(
        diagnostic_step(),
        {
            "agent/verification/workers/captures.json": json.dumps(
                [{"name": DIAGNOSTIC_WORKER_NAME, "is_overflow": True, "state": None, "is_stream_captured": None}]
            )
        },
    )

    facts = _flat_trial_facts(tmp_path, step)

    assert facts.values["workers.discovered"] == [DIAGNOSTIC_WORKER_NAME]
    assert facts.values["workers.captured"] == []


# --- the delivered repo ---


def test_the_bundle_is_verified_against_the_captured_file(tmp_path: Path) -> None:
    """The bundle is incremental against a commit no check-time repo can hold, so verifying it has to
    accept the missing prerequisite and nothing else."""
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    _git(source_dir, "init", "--quiet", ".")
    _git(source_dir, "config", "user.email", "diagnostics@example.com")
    _git(source_dir, "config", "user.name", "diagnostics")
    (source_dir / "index.html").write_text("<title>Todo</title>")
    _git(source_dir, "add", "-A")
    _git(source_dir, "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "base")
    base_sha = (
        subprocess.run(["git", "-C", str(source_dir), "rev-parse", "HEAD"], capture_output=True, check=True)
        .stdout.decode()
        .strip()
    )
    (source_dir / "index.html").write_text("<title>Todo</title><p>walk dog</p>")
    _git(source_dir, "add", "-A")
    _git(source_dir, "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "bootstrap")
    bundle_path = tmp_path / "deliverable.bundle"
    _git(source_dir, "bundle", "create", str(bundle_path), "{}..HEAD".format(base_sha))

    step = edited_diagnostic_step(
        diagnostic_step(authored_expectations=_MINDS_APP_EXPECTATIONS),
        {
            "agent/verification/repo_state.json": json.dumps(
                {
                    "repo_root": "/home/user/workspace",
                    "base_sha": base_sha,
                    "head_sha": "b" * 40,
                    "commit_count_beyond_base": "1",
                    "agent_commit_count": 0,
                    "bootstrap_commit_count": 1,
                    "is_clean": True,
                    "status_porcelain": "",
                }
            )
        },
    )
    trial_dir = write_diagnostic_trial_dir(tmp_path / "job", "behaviour__aaaaaaa", [step])
    # The bundle is binary, so it is copied in rather than written as one of the step's texts.
    (trial_dir / "agent" / "verification" / "deliverable.bundle").write_bytes(bundle_path.read_bytes())

    (facts,) = compute_trial_facts(trial_dir, tmp_path / "work").steps

    assert facts.values["bundle.verified"] is True
    assert facts.values["bundle.bytes"] == bundle_path.stat().st_size
    assert facts.values["repo.agent_commit_count"] == 0
    assert facts.values["repo.bootstrap_commit_count"] == 1


def test_a_file_that_is_not_a_bundle_does_not_verify(tmp_path: Path) -> None:
    assert fact_sources.is_bundle_verified(_written(tmp_path / "not.bundle", "nonsense"), tmp_path / "work") is False


def test_a_bundle_named_relative_to_the_caller_verifies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A job directory is normally given as a path relative to the repo root, and the bundle inside it
    inherits that; verify runs in a repo of its own, where such a path names nothing."""
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    _git(source_dir, "init", "--quiet", "--initial-branch", "main")
    _written(source_dir / "first.txt", "first")
    _git(source_dir, "add", "-A")
    _git(source_dir, "-c", "user.email=a@b.c", "-c", "user.name=A", "commit", "-q", "-m", "first")
    bundle_path = tmp_path / "deliverable.bundle"
    _git(source_dir, "bundle", "create", str(bundle_path), "HEAD")
    monkeypatch.chdir(tmp_path)

    assert fact_sources.is_bundle_verified(Path("deliverable.bundle"), tmp_path / "work") is True


def test_a_trial_that_committed_nothing_owes_no_bundle(tmp_path: Path) -> None:
    step = edited_diagnostic_step(
        diagnostic_step(authored_expectations=_MINDS_APP_EXPECTATIONS),
        {"agent/verification/repo_state.json": json.dumps({"base_sha": "a" * 40, "commit_count_beyond_base": "0"})},
    )

    facts = _flat_trial_facts(tmp_path, step)

    assert "bundle.bytes" not in facts.values and "bundle.verified" not in facts.values


def test_a_bundle_that_was_due_and_never_captured_is_not_recorded(tmp_path: Path) -> None:
    step = edited_diagnostic_step(
        diagnostic_step(authored_expectations=_MINDS_APP_EXPECTATIONS),
        {"agent/verification/repo_state.json": json.dumps({"base_sha": "a" * 40, "commit_count_beyond_base": "3"})},
    )

    facts = _flat_trial_facts(tmp_path, step)

    assert facts.values["bundle.bytes"] is NOT_RECORDED
    assert facts.values["bundle.verified"] is NOT_RECORDED


# --- snapshots ---


def test_a_snapshot_the_pull_lost_records_its_zero_and_no_tarball_to_read(tmp_path: Path) -> None:
    """The driver records zero bytes for a pull that failed and null for a trial that pulled none, so
    a lost snapshot is a fact rather than a silence."""
    step = edited_diagnostic_step(
        diagnostic_step(), {"agent/state.json": json.dumps(diagnostic_state(snapshot_byte_count=0))}
    )

    facts = _flat_trial_facts(tmp_path, step)

    assert facts.values["snapshot.bytes"] == 0
    assert facts.values["snapshot.readable"] is NOT_RECORDED


def test_a_pulled_snapshot_is_readable_when_its_tarball_lists_a_workspace_tree(tmp_path: Path) -> None:
    """Written the way the workspace writes one, `./`-prefixed members and all: a reader that wants
    a bare `workspace/` would call every real snapshot unreadable."""
    snapshot_path = tmp_path / "snapshot.tar.gz"
    byte_count = write_workspace_snapshot(snapshot_path, tmp_path / "tree")
    step = edited_diagnostic_step(
        diagnostic_step(), {"agent/state.json": json.dumps(diagnostic_state(snapshot_byte_count=byte_count))}
    )
    trial_dir = write_diagnostic_trial_dir(tmp_path / "job", "behaviour__aaaaaaa", [step])
    (trial_dir / "agent" / fact_sources.SNAPSHOTS_DIRNAME).mkdir(parents=True)
    (trial_dir / "agent" / fact_sources.SNAPSHOTS_DIRNAME / "post_message_1.tar.gz").write_bytes(
        snapshot_path.read_bytes()
    )

    (facts,) = compute_trial_facts(trial_dir, tmp_path / "work").steps

    assert facts.values["snapshot.readable"] is True
    assert facts.values["snapshot.bytes"] == byte_count


def test_a_tarball_that_is_not_a_workspace_tree_is_not_readable_as_one(tmp_path: Path) -> None:
    other_path = tmp_path / "other.tar.gz"
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "notes.txt").write_text("nothing to do with a workspace")
    with tarfile.open(other_path, "w:gz") as archive:
        archive.add(tmp_path / "elsewhere", arcname=".")
    step = edited_diagnostic_step(
        diagnostic_step(), {"agent/state.json": json.dumps(diagnostic_state(snapshot_byte_count=10))}
    )
    trial_dir = write_diagnostic_trial_dir(tmp_path / "job", "behaviour__aaaaaaa", [step])
    (trial_dir / "agent" / fact_sources.SNAPSHOTS_DIRNAME).mkdir(parents=True)
    (trial_dir / "agent" / fact_sources.SNAPSHOTS_DIRNAME / "post_message_1.tar.gz").write_bytes(
        other_path.read_bytes()
    )

    (facts,) = compute_trial_facts(trial_dir, tmp_path / "work").steps

    assert facts.values["snapshot.readable"] is False


def test_the_last_snapshot_pulled_is_the_one_read(tmp_path: Path) -> None:
    """The driver stamps each snapshot with its turn and does not pad the number, so name order puts
    the tenth before the second and the fact would describe a tarball the byte count does not."""
    snapshots_dir = tmp_path / "snapshots"
    snapshots_dir.mkdir()
    write_workspace_snapshot(snapshots_dir / "post_message_10.tar.gz", tmp_path / "tenth")
    (snapshots_dir / "post_message_2.tar.gz").write_text("not a tarball at all")

    assert fact_sources.snapshot_member_names(snapshots_dir / "post_message_10.tar.gz") is not None
    assert fact_sources._newest_snapshot_path(snapshots_dir) == snapshots_dir / "post_message_10.tar.gz"


# --- flows ---


def test_a_flows_facts_come_from_its_log_its_frames_and_its_run_record(tmp_path: Path) -> None:
    records = [
        {
            "kind": "init",
            "step_index": 0,
            "url": "https://todo.localhost/",
            "state": 'page https://todo.localhost/ (Todo)\n- checkbox "Buy milk"',
        },
        {
            "kind": "action",
            "step_index": 1,
            "action": "click the button named 'Add'",
            "target_ref": "e9",
            "reaction": "settled",
            "observed": "the page changed",
            "state": 'page https://todo.localhost/ (Todo)\n- checkbox "Buy milk"',
        },
        {
            "kind": "action",
            "step_index": 2,
            "action": "finish the flow",
            "state": 'page https://todo.localhost/ (Todo)\n- checkbox "Buy milk"\n- checkbox "walk dog" [checked]',
        },
        {"kind": "final", "step_index": 3, "state": "page https://todo.localhost/ (Todo)"},
    ]
    step = diagnostic_step(authored_expectations=_MINDS_APP_EXPECTATIONS)
    step = edited_diagnostic_step(
        step,
        {
            "agent/verification/flows/plain/log.jsonl": "".join(json.dumps(record) + "\n" for record in records),
            "agent/verification/flows/plain/run.json": json.dumps(
                {"status": "passed", "reason": "", "verifier_call_count": 3}
            ),
            "agent/verification/flows/plain/step_000.png": "frame",
            "agent/verification/flows/plain/step_001.png": "frame",
        },
    )

    facts = _flat_trial_facts(tmp_path, step)

    assert facts.values["flow.plain.status"] == "passed"
    assert facts.values["flow.plain.record_kinds"] == ["init", "action", "action", "final"]
    assert facts.values["flow.plain.png_count"] == 2
    assert facts.values["flow.plain.verifier_call_count"] == 3
    assert facts.values["flow.plain.ref_step_count"] == 1
    assert facts.values["flow.plain.step.1.addressed_by_ref"] is True
    assert facts.values["flow.plain.step.1.observed_kind"] == "changed"
    assert facts.values["flow.plain.after.1.checkboxes"] == ["Buy milk", "walk dog"]
    assert facts.values["flow.plain.after.1.checked"] == ["walk dog"]
    assert facts.values["flow.plain.reaction_counts"] == {"settled": 1}
    assert facts.values["judge.digest_flow_count"] == 0
    assert facts.values["judge.unnamed_control_note"] is False
    assert facts.values["judge.addressed_by_ref_step_count"] == 0


def test_the_judge_facts_read_the_digest_the_renderer_wrote(tmp_path: Path) -> None:
    """The digest the facts read is rendered rather than written by hand, so a fact can only agree
    with the marks a judge actually sees."""
    renderer = load_template_module("tests/verifier/render_flow_evidence.py", "minds_evals_rendered_digest")
    verification_dir = tmp_path / "verification"
    flow_dir = verification_dir / "flows" / "plain"
    flow_dir.mkdir(parents=True)
    (flow_dir / "log.jsonl").write_text(
        "".join(
            json.dumps(record) + "\n"
            for record in (
                {"kind": "init", "step_index": 0, "state": ""},
                {"kind": "action", "step_index": 1, "action": "click the checkbox", "target_ref": "e12", "state": ""},
                {"kind": "action", "step_index": 2, "action": "click the button named 'Add'", "state": ""},
            )
        )
    )
    digest_path = tmp_path / "judge_flows_digest.txt"
    renderer.collect_flow_evidence(verification_dir, digest_path, tmp_path / "shots", tmp_path / "case.json")
    step = edited_diagnostic_step(
        diagnostic_step(authored_expectations=_MINDS_APP_EXPECTATIONS),
        {"verifier/derived/judge_flows_digest.txt": digest_path.read_text()},
    )

    facts = _flat_trial_facts(tmp_path / "job", step)

    assert facts.values["judge.digest_flow_count"] == 1
    assert facts.values["judge.unnamed_control_note"] is True
    assert facts.values["judge.addressed_by_ref_step_count"] == 1


def test_a_flow_status_falls_back_to_the_manifest_entry_that_indexes_it(tmp_path: Path) -> None:
    """The run record says how a flow ended, but a flow whose collection died before writing one is
    still indexed by the manifest."""
    step = diagnostic_step(authored_expectations=_MINDS_APP_EXPECTATIONS)
    step = edited_diagnostic_step(
        step,
        {
            "agent/verification/manifest.json": json.dumps(
                diagnostic_manifest(
                    entries=[
                        {
                            "entry_id": "ui_flow_0_plain",
                            "check_class": "ui_flows",
                            "status": "error",
                            "env": "live",
                            "reason": "verifier_agent_failed",
                            "detail": "",
                            "evidence_path": "verification/flows/plain",
                        }
                    ]
                )
            ),
            "agent/verification/flows/plain/log.jsonl": "",
        },
    )

    facts = _flat_trial_facts(tmp_path, step)

    assert facts.values["flow.plain.status"] == "error"
    assert facts.values["evidence.errored_entries"] == ["ui_flow_0_plain"]


def test_read_page_state_reads_a_nameless_checkbox_as_the_empty_name() -> None:
    """The fixture's `?unnamed=1` page gives its checkboxes no accessible name, which is the control a
    ref-addressed action exists for."""
    reading = read_page_state(
        "page https://todo.localhost/?unnamed=1 (Todo)\n- checkbox [ref=e9] [checked]\n- checkbox [ref=e10]"
    )

    assert reading.checkboxes == ("", "")
    assert reading.checked == ("",)
    assert reading.url_query == "unnamed=1"


def test_a_decision_the_executor_could_not_carry_out_is_not_one_of_the_flows_actions() -> None:
    """An unusable decision is the verification agent failing, so folding it into the reaction tally
    would read as the app not answering."""
    records = [
        {"kind": "init", "step_index": 0, "state": "page https://todo.localhost/ (Todo)"},
        {"kind": "action", "step_index": 1, "action": "click the button named 'Add'", "reaction": "settled"},
        {"kind": "action", "step_index": 2, "action": ui_flows.UNUSABLE_ACTION, "reaction": "unobserved"},
    ]

    facts = flow_facts("plain", "error", records, frame_count=2, verifier_call_count=3)

    assert facts["flow.plain.reaction_counts"] == {"settled": 1}
    assert facts["flow.plain.no_reaction_share"] == 0.0
    assert "flow.plain.step.2.reaction" not in facts
    # The record is still one of the flow's lines: the log says what happened, the tally says what the app did.
    assert facts["flow.plain.record_kinds"] == ["init", "action", "action"]


# --- the verifier's reading of the step ---


def test_the_gate_results_name_every_criterion_the_verifier_scored(tmp_path: Path) -> None:
    """A gate deleted or renamed is a miss whatever the others say, so the facts carry the names and
    not just the verdict."""
    facts = _flat_trial_facts(tmp_path, diagnostic_step())

    assert facts.values["gates.held"] is True
    assert facts.values["gates.results"] == {
        "agent_engaged_substantively": True,
        "all_turns_completed": True,
        "not_timed_out": True,
        "transcript_has_agent_reply": True,
    }


def test_a_harness_the_verifier_read_differently_from_the_lane_is_a_disagreement(tmp_path: Path) -> None:
    """A pi or codex trial whose document fell back to hand-built and whose accounts listing named no
    harness is graded as claude, which only a second record can catch."""
    step = diagnostic_step(harness="codex")
    step = edited_diagnostic_step(
        step,
        {
            "verifier/reward-details.json": json.dumps(
                {
                    "gates": {"kind": "programmatic", "criteria": [{"name": "not_timed_out", "value": 1.0}]},
                    "harness": {"name": "claude"},
                }
            )
        },
    )

    facts = _flat_trial_facts(tmp_path, step)

    assert facts.values["harness.detected"] == "claude"
    assert facts.values["harness.detected_is_lane_harness"] is False


def test_a_step_with_no_verifier_output_records_none_of_its_readings(tmp_path: Path) -> None:
    step = edited_diagnostic_step(diagnostic_step(), {"verifier/reward-details.json": None})

    facts = _flat_trial_facts(tmp_path, step)

    assert facts.values["gates.held"] is NOT_RECORDED
    assert facts.values["harness.detected_is_lane_harness"] is NOT_RECORDED
    assert facts.values["trial.completed"] is True


def test_the_usage_facts_come_from_the_transcript_account_harbor_recorded(tmp_path: Path) -> None:
    """The proxy's account holds delegated work whole by construction, so its completeness says
    nothing about the capture; the transcript's is what the fact reads."""
    step = diagnostic_step()
    incomplete_metadata = {
        "transcript_usage": {
            "message_count": 0,
            "is_cost_complete": False,
            "worker_launch_count": 1,
            "worker_captured_count": 0,
        },
        "workspace_usage": {"tokens": {"input": 1_000}},
    }
    step = step.model_copy_update(to_update(step.field_ref().metadata, incomplete_metadata))

    facts = _flat_trial_facts(tmp_path, step)

    assert facts.values["usage.tokens_present"] is False
    assert facts.values["usage.is_cost_complete"] is False
    assert facts.values["workers.launch_count"] == 1
    assert facts.values["workers.captured_count"] == 0


def test_the_client_messages_are_where_the_transcript_facts_start_counting(tmp_path: Path) -> None:
    """The template greets before the first client turn, so an agent step before it belongs to the
    greeting rather than to the conversation under test."""
    document = diagnostic_trajectory()
    document["steps"] = [
        {"step_id": 1, "source": "agent", "message": "Hello!"},
        {"step_id": 2, "source": "user", "message": DIAGNOSTIC_CLIENT_MESSAGE},
        {"step_id": 3, "source": "agent", "message": "Done.", "model_name": "claude-haiku-4-5"},
    ]
    step = edited_diagnostic_step(diagnostic_step(), {"agent/trajectory.json": json.dumps(document)})

    facts = _flat_trial_facts(tmp_path, step)

    assert facts.values["transcript.user_step_count"] == 1
    assert facts.values["transcript.agent_steps_with_model_name"] == "all"


def _trajectory_embedding(worker_name: str) -> dict[str, Any]:
    """The published document with one worker's trajectory grafted into it, as the capture grafts it."""
    document = diagnostic_trajectory()
    document["subagent_trajectories"] = [
        {"trajectory_id": "sub-1", "steps": [], "extra": {"worker": {"name": worker_name}}}
    ]
    return document


def _listing_json(worker_name: str, agent_type: str) -> str:
    """`mngr list --format json`, naming one agent-created worker of the given type."""
    return json.dumps(
        {
            "agents": [
                {
                    "id": "agent-{}".format(worker_name),
                    "name": worker_name,
                    "type": agent_type,
                    "state": "running",
                    "work_dir": "/home/user/workspace",
                    "labels": {"agent_created": "true"},
                }
            ],
            "errors": [],
        }
    )


def _git(repo_dir: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(repo_dir), *arguments], capture_output=True, check=True)


def _written(path: Path, contents: str) -> Path:
    path.write_text(contents)
    return path


def test_the_state_keys_the_facts_read_are_the_ones_the_driver_writes() -> None:
    """Spelled in both places rather than shared, so that a renamed key is caught here instead of
    reading as a trial that recorded nothing."""
    payload_source = inspect.getsource(MindsPersonaDriver._state_payload)

    assert [key for key in STATE_KEYS_READ if '"{}"'.format(key) not in payload_source] == []


def test_the_digest_markers_the_facts_read_are_the_ones_the_renderer_writes() -> None:
    """The renderer is a stdlib script that runs in the verifier container, so its markers are
    spelled again here; a reworded marker has to be caught by this rather than read as a digest that
    marked nothing."""
    renderer = load_template_module("tests/verifier/render_flow_evidence.py", "minds_evals_digest_markers")
    renderer_source = inspect.getsource(renderer)

    assert [marker for marker in DIGEST_MARKERS_READ if marker not in renderer_source] == []


def test_a_document_that_is_json_but_not_a_trajectory_answers_nothing(tmp_path: Path) -> None:
    """A step list is what every transcript reading is taken from, so a file that carries none must
    not answer "every tool call has its result" and "the markers match the case" with a yes."""
    step = edited_diagnostic_step(
        diagnostic_step(), {"agent/trajectory.json": json.dumps({"extra": {"minds_evals": {"source": "workspace"}}})}
    )

    facts = _flat_trial_facts(tmp_path, step)

    assert facts.values["tools.every_call_has_one_result"] is NOT_RECORDED
    assert facts.values["steps.boundary_markers_match_case"] is NOT_RECORDED
    assert facts.values["transcript.source"] is NOT_RECORDED


def test_the_step_says_whether_its_own_case_declaration_could_be_read(tmp_path: Path) -> None:
    """The instruction is what says which checks the step owed, so losing it must not turn every
    declared check into one the case never declared."""
    step = diagnostic_step(authored_expectations=_MINDS_APP_EXPECTATIONS)

    read = _flat_trial_facts(tmp_path / "read", step)
    unread = _flat_trial_facts(tmp_path / "unread", edited_diagnostic_step(step, {"agent/instruction.md": None}))

    assert read.values["case.readable"] is True
    assert unread.values["case.readable"] is False
    assert "http.http_1_todo_fixture.status" not in unread.values


def test_the_delivered_apps_are_read_off_the_registry_and_its_services(tmp_path: Path) -> None:
    """The row is joined to its program through the supervisord config's forward_port call, not by
    assuming the two share a name."""
    step = edited_diagnostic_step(
        diagnostic_step(),
        {
            "agent/verification/apps.toml": (
                '[[apps]]\nname = "terminal"\nurl = "http://localhost:7000/"\n'
                '[[apps]]\nname = "todo"\nurl = "http://localhost:8090/"\n'
            ),
            "agent/verification/services.txt": (
                "terminal    RUNNING   pid 1, uptime 0:01:00\ntodo-app    RUNNING   pid 2, uptime 0:00:30\n"
            ),
            "agent/verification/supervisord.conf": (
                "[program:terminal]\ncommand = forward_port.py --name terminal\n"
                "[program:todo-app]\ncommand = forward_port.py --name todo --url http://localhost:8090\n"
            ),
        },
    )

    facts = _flat_trial_facts(tmp_path, step)

    assert facts.values["apps.delivered"] == ["todo"]
    assert facts.values["apps.service_state.todo"] == "RUNNING"
    assert facts.values["apps.registered_all_running"] is True


def test_an_app_whose_service_crashed_is_not_running(tmp_path: Path) -> None:
    step = edited_diagnostic_step(
        diagnostic_step(),
        {"agent/verification/services.txt": "terminal    FATAL     Exited too quickly\n"},
    )

    facts = _flat_trial_facts(tmp_path, step)

    assert facts.values["apps.service_state.terminal"] == "FATAL"
    assert facts.values["apps.registered_all_running"] is False


def test_the_delivered_apps_are_not_recorded_without_the_manifest_that_says_what_was_preexisting(
    tmp_path: Path,
) -> None:
    """The registry alone cannot say which rows the workspace already served, and the manifest that
    can is the record that went missing -- which is not the same claim as a set that cannot be
    resolved from records that are all there."""
    step = edited_diagnostic_step(diagnostic_step(), {"agent/verification/manifest.json": None})

    facts = _flat_trial_facts(tmp_path, step)

    assert facts.values["apps.delivered"] is NOT_RECORDED
    assert facts.values["apps.registered_all_running"] is NOT_RECORDED

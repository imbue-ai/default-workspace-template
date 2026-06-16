"""Unit tests for the window error watcher's pure core.

The `mngr` argv builders are additionally confronted with the live
`imbue.mngr.main.cli` tree via `assert_mngr_argv_valid`, so a vendor/mngr rename
of the `list`/`message` subcommand or one of its flags fails here at merge time.
"""

import json
import random

from mngr_cli_contract.contract import assert_mngr_argv_valid

from error_watcher.watcher import (
    DEFAULT_ERROR_PATTERN,
    AgentSummary,
    build_list_command,
    build_message_command,
    choose_recipient,
    format_alert,
    match_lines,
    new_matches,
    parse_agent_summaries,
)


def test_match_lines_is_case_insensitive() -> None:
    text = "all good\nError: boom\nEXCEPTION raised\nstill fine"
    assert match_lines(text, DEFAULT_ERROR_PATTERN) == [
        "Error: boom",
        "EXCEPTION raised",
    ]


def test_match_lines_matches_traceback_exception() -> None:
    text = "Traceback (most recent call last):\n  File ...\nValueError: bad\nException: nope"
    matched = match_lines(text, DEFAULT_ERROR_PATTERN)
    assert "Exception: nope" in matched


def test_match_lines_returns_empty_for_clean_output() -> None:
    assert (
        match_lines("compiled successfully\nall tests passed\n", DEFAULT_ERROR_PATTERN)
        == []
    )


def test_match_lines_naively_matches_benign_substrings() -> None:
    # v1 is deliberately naive: "0 errors" and "ErrorBoundary" both contain
    # "error", so they match. This documents the spec's stated non-goal.
    text = "0 errors\nrendered <ErrorBoundary>\nok"
    assert match_lines(text, DEFAULT_ERROR_PATTERN) == [
        "0 errors",
        "rendered <ErrorBoundary>",
    ]


def test_new_matches_returns_and_records_on_first_sight() -> None:
    seen: dict[str, set[str]] = {}
    assert new_matches("svc-web", ["Error: boom"], seen) == ["Error: boom"]
    assert seen["svc-web"] == {"Error: boom"}


def test_new_matches_suppresses_already_alerted_lines() -> None:
    seen: dict[str, set[str]] = {}
    new_matches("svc-web", ["Error: boom"], seen)
    # The same error still on screen on the next poll must not re-alert.
    assert new_matches("svc-web", ["Error: boom"], seen) == []


def test_new_matches_returns_only_the_newly_appeared_line() -> None:
    seen: dict[str, set[str]] = {}
    new_matches("svc-web", ["Error: boom"], seen)
    assert new_matches("svc-web", ["Error: boom", "Exception: later"], seen) == [
        "Exception: later"
    ]


def test_new_matches_tracks_windows_independently() -> None:
    seen: dict[str, set[str]] = {}
    new_matches("svc-web", ["Error: boom"], seen)
    # The same text in a different window is new for that window.
    assert new_matches("svc-api", ["Error: boom"], seen) == ["Error: boom"]


def test_new_matches_deduplicates_within_a_single_capture() -> None:
    seen: dict[str, set[str]] = {}
    assert new_matches("svc-web", ["Error: boom", "Error: boom"], seen) == [
        "Error: boom"
    ]


def test_format_alert_includes_session_window_and_line() -> None:
    message = format_alert("agent-session", {"svc-web": ["Error: boom"]})
    assert "agent-session" in message
    assert "svc-web" in message
    assert "Error: boom" in message


def test_format_alert_batches_multiple_windows_into_one_message() -> None:
    message = format_alert(
        "agent-session",
        {"svc-web": ["Error: boom"], "svc-api": ["Exception: a", "Exception: b"]},
    )
    assert "svc-web" in message
    assert "svc-api" in message
    assert "Exception: a | Exception: b" in message


def test_format_alert_truncates_overlong_lines() -> None:
    long_line = "Error " + "x" * 1000
    message = format_alert("agent-session", {"svc-web": [long_line]})
    assert "..." in message
    assert len(long_line) not in {len(part) for part in message.splitlines()}


def test_list_command_is_accepted_by_live_cli() -> None:
    argv = build_list_command()
    assert argv == ["mngr", "list", "--format", "json"]
    assert_mngr_argv_valid(argv)


def test_message_command_is_accepted_by_live_cli() -> None:
    argv = build_message_command("demo-agent", "something errored")
    assert argv == ["mngr", "message", "demo-agent", "-m", "something errored"]
    assert_mngr_argv_valid(argv)


def test_parse_agent_summaries_reads_name_and_state() -> None:
    payload = json.dumps(
        {
            "agents": [
                {
                    "resource_type": "agent",
                    "name": "agent-web",
                    "type": "claude",
                    "state": "RUNNING",
                },
                {"name": "agent-api", "type": "claude", "state": "STOPPED"},
            ],
            "errors": [],
        }
    )
    assert parse_agent_summaries(payload) == [
        AgentSummary(name="agent-web", state="RUNNING"),
        AgentSummary(name="agent-api", state="STOPPED"),
    ]


def test_parse_agent_summaries_skips_agents_missing_name_or_state() -> None:
    payload = json.dumps(
        {
            "agents": [
                {"name": "agent-web", "state": "RUNNING"},
                {"name": "", "state": "RUNNING"},
                {"name": "agent-api"},
                "not-a-dict",
            ]
        }
    )
    assert parse_agent_summaries(payload) == [
        AgentSummary(name="agent-web", state="RUNNING")
    ]


def test_parse_agent_summaries_returns_empty_on_malformed_json() -> None:
    assert parse_agent_summaries("this is not json") == []


def test_parse_agent_summaries_returns_empty_when_not_an_object() -> None:
    assert parse_agent_summaries("[1, 2, 3]") == []


def test_parse_agent_summaries_returns_empty_when_agents_not_a_list() -> None:
    assert parse_agent_summaries(json.dumps({"agents": "nope"})) == []


def test_choose_recipient_is_deterministic_for_a_seeded_rng() -> None:
    # random.Random(0).choice(["alpha", "beta", "gamma"]) is "beta".
    assert choose_recipient(["alpha", "beta", "gamma"], random.Random(0)) == "beta"


def test_choose_recipient_returns_none_for_empty_pool() -> None:
    assert choose_recipient([], random.Random(0)) is None

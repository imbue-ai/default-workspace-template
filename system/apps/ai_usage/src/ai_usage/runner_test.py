import json
import time
from pathlib import Path

from app_schema.testing import assert_api_schema_describes_app

from ai_usage.providers import antigravity, claude
from ai_usage.runner import app
from ai_usage.usage import (
    AgentInfo,
    Attribution,
    Source,
    UsageStore,
    chat_id_of,
    chats_to_show,
    clean_prompt,
)


def test_the_app_describes_its_api_the_way_its_manifest_says() -> None:
    assert_api_schema_describes_app(
        app.test_client(), Path(__file__).resolve().parents[2] / "app.toml"
    )


def test_antigravity_parses_bucket_pools_and_inverts_remaining_fraction() -> None:
    raw = {
        "buckets": [
            {"tokenType": "WTUS", "modelId": "chat_20706", "remainingFraction": 1},
            {
                "resetTime": "2026-11-18T01:08:07Z",
                "tokenType": "WTUS",
                "modelId": "gemini-2.5-pro",
                "remainingFraction": 0.9455,
            },
            {
                "resetTime": "2026-11-18T01:09:49Z",
                "tokenType": "WTUS",
                "modelId": "claude-opus-5-5-high",
                "remainingFraction": 0.75,
            },
        ]
    }
    limits = {limit.key: limit for limit in antigravity.parse_antigravity_limits(raw)}
    assert set(limits) == {"gemini", "claude_gpt"}
    assert limits["gemini"].percent == 5.45
    assert limits["gemini"].resets_at == "2026-11-18T01:08:07Z"
    assert limits["claude_gpt"].percent == 25.0
    assert limits["claude_gpt"].resets_at == "2026-11-18T01:09:49Z"


def test_antigravity_reads_json_and_raw_token_files(tmp_path: Path) -> None:
    token_file = tmp_path / "antigravity-oauth-token"
    provider = antigravity.PROVIDER.model_copy(
        update={"credentials_paths": lambda _account: [token_file]}
    )
    token_file.write_text(
        json.dumps(
            {"token": {"access_token": "ya29.json-token"}, "auth_method": "consumer"}
        )
    )
    creds = provider.credentials("google")
    assert creds is not None
    assert provider.access_token(creds) == "ya29.json-token"

    token_file.write_text("ya29.raw-token\n")
    raw_creds = provider.credentials("google")
    assert raw_creds is not None
    assert provider.access_token(raw_creds) == "ya29.raw-token"


def test_clean_prompt_strips_handoff_and_skips_system_messages() -> None:
    wrapped = (
        "You are continuing the chat...\n\n"
        "The user's message, which you are answering now:\n\n"
        "draft the antigravity file"
    )
    assert clean_prompt(wrapped) == "draft the antigravity file"
    assert clean_prompt("The following is a <SYSTEM_MESSAGE> not sent by user") is None


def _store_with_one_credited_turn(
    tmp_path: Path, prompt: str
) -> tuple[UsageStore, Source]:
    """A store whose logs hold one Claude chat's message and a usage increase while it worked."""
    store = UsageStore(tmp_path)
    store._agents = [
        AgentInfo(chat_id="chat-1", name="Chat", agent_type="claude", account="acct")
    ]
    store._agents_at = time.time() + 3600
    activity = [
        {
            "t": "2026-11-17T10:00:00+00:00",
            "chat_id": "chat-1",
            "event": "prompt",
            "prompt": prompt,
        },
        {"t": "2026-11-17T10:05:00+00:00", "chat_id": "chat-1", "event": "stop"},
    ]
    readings = [
        {
            "t": t,
            "status": 200,
            "source_key": "claude:acct",
            "raw": {
                "five_hour": {
                    "utilization": used,
                    "resets_at": "2026-11-17T15:00:00+00:00",
                }
            },
        }
        for t, used in (
            ("2026-11-17T09:59:00+00:00", 10),
            ("2026-11-17T10:04:00+00:00", 14),
        )
    ]
    store.activity_path.write_text("".join(json.dumps(r) + "\n" for r in activity))
    store.readings_path.write_text("".join(json.dumps(r) + "\n" for r in readings))
    source = Source(
        key="claude:acct",
        provider=claude.PROVIDER,
        account="acct",
        account_label="acct",
    )
    return store, source


LONG_PROMPT = "Please go through the whole sales spreadsheet and chart revenue by region for each quarter"


def test_a_turn_is_labelled_with_its_summary_and_keeps_the_verbatim_message(
    tmp_path: Path,
) -> None:
    store, source = _store_with_one_credited_turn(tmp_path, LONG_PROMPT)
    store._summariser = lambda prompt: "Chart revenue by region"
    store.request_missing_summaries()
    store._summary_pool.shutdown(wait=True)

    turn = store.attribute(source).turns["chat-1"][0]
    assert (turn.label, turn.prompt, turn.summarised) == (
        "Chart revenue by region",
        LONG_PROMPT,
        True,
    )
    # The summary is kept on disk, so a restart shows it without asking the AI again.
    assert UsageStore(tmp_path).summary_for(LONG_PROMPT) == "Chart revenue by region"


def test_a_turn_without_a_summary_is_labelled_with_the_message_itself(
    tmp_path: Path,
) -> None:
    store, source = _store_with_one_credited_turn(tmp_path, LONG_PROMPT)
    store._summariser = lambda prompt: None
    store.request_missing_summaries()
    store._summary_pool.shutdown(wait=True)

    turn = store.attribute(source).turns["chat-1"][0]
    assert (turn.label, turn.prompt, turn.summarised) == (
        LONG_PROMPT,
        LONG_PROMPT,
        False,
    )
    assert not store.summaries_path.exists()


def test_a_short_message_is_never_sent_to_the_ai(tmp_path: Path) -> None:
    store, _source = _store_with_one_credited_turn(tmp_path, "fix the login page")
    asked: list[str] = []
    store._summariser = lambda prompt: asked.append(prompt) or "x"
    store.request_missing_summaries()
    store._summary_pool.shutdown(wait=True)
    assert asked == []


def test_a_chat_that_never_moved_between_ais_is_known_by_its_own_agent_id() -> None:
    new_chat = {"id": "agent-new", "type": "claude", "labels": {"display_name": "New chat"}}
    moved_chat = {"id": "agent-b", "type": "antigravity", "labels": {"display_name": "Main", "chat_id": "agent-a"}}
    assert chat_id_of(new_chat) == "agent-new"
    assert chat_id_of(moved_chat) == "agent-a"


def test_agents_that_are_not_chats_are_left_out() -> None:
    services = {"id": "agent-s", "type": "main", "labels": {"is_primary": "true"}}
    room = {"id": "agent-r", "type": "room", "labels": {"display_name": "Team room"}}
    assert chat_id_of(services) is None
    assert chat_id_of(room) is None


def test_a_reset_time_that_wobbles_by_a_fraction_of_a_second_is_not_a_new_window(tmp_path: Path) -> None:
    store, source = _store_with_one_credited_turn(tmp_path, LONG_PROMPT)
    readings = [
        {"t": t, "status": 200, "source_key": "claude:acct", "raw": {"five_hour": {"utilization": used, "resets_at": resets}}}
        for t, used, resets in (
            ("2026-11-17T09:59:00+00:00", 0, "2026-11-17T15:00:00.581527+00:00"),
            ("2026-11-17T10:01:00+00:00", 1, "2026-11-17T15:00:00.460939+00:00"),
            ("2026-11-17T10:02:00+00:00", 1, "2026-11-17T14:59:59.983440+00:00"),
            ("2026-11-17T10:03:00+00:00", 1, "2026-11-17T15:00:01.006727+00:00"),
        )
    ]
    store.readings_path.write_text("".join(json.dumps(r) + "\n" for r in readings))
    assert store.attribute(source).totals == {"chat-1": {"five_hour": 1.0}}


def test_a_limit_that_really_reset_counts_everything_used_since(tmp_path: Path) -> None:
    store, source = _store_with_one_credited_turn(tmp_path, LONG_PROMPT)
    readings = [
        {"t": t, "status": 200, "source_key": "claude:acct", "raw": {"five_hour": {"utilization": used, "resets_at": resets}}}
        for t, used, resets in (
            ("2026-11-17T09:59:00+00:00", 40, "2026-11-17T10:00:30+00:00"),
            ("2026-11-17T10:03:00+00:00", 2, "2026-11-17T15:00:30+00:00"),
        )
    ]
    store.readings_path.write_text("".join(json.dumps(r) + "\n" for r in readings))
    assert store.attribute(source).totals == {"chat-1": {"five_hour": 2.0}}


def test_an_apps_own_assistant_gets_a_tab_only_once_it_has_used_something() -> None:
    mine = AgentInfo(chat_id="mine", name="chat1", agent_type="claude", account="acct")
    files = AgentInfo(chat_id="files", name="Files Chat", agent_type="claude", account="acct", built_in=True)
    unused = Attribution(series={}, totals={}, turns={})
    used = Attribution(series={}, totals={"files": {"seven_day": 0.5}}, turns={})
    assert chats_to_show([mine, files], unused) == [mine]
    assert chats_to_show([mine, files], used) == [mine, files]


def test_antigravity_shows_weekly_and_five_hour_limits_and_keeps_a_pool_the_summary_omits() -> None:
    raw = {
        "buckets": [
            {"resetTime": "2026-11-18T05:00:00Z", "modelId": "gemini-2.5-pro", "remainingFraction": 0.9},
            {"resetTime": "2026-11-18T05:00:00Z", "modelId": "claude-opus-5-5-high", "remainingFraction": 0.5},
        ],
        "quotaSummary": {
            "groups": [
                {
                    "displayName": "Claude and GPT models",
                    "buckets": [
                        {"bucketId": "3p-weekly", "window": "weekly", "resetTime": "2026-11-24T23:59:31Z", "remainingFraction": 0.8},
                        {"bucketId": "3p-5h", "window": "5h", "resetTime": "2026-11-18T04:59:31Z", "remainingFraction": 0.5},
                    ],
                }
            ]
        },
    }
    limits = {limit.key: limit for limit in antigravity.parse_antigravity_limits(raw)}
    assert list(limits) == ["3p_weekly", "3p_5h", "gemini"]
    assert limits["3p_weekly"].label == "Claude and GPT models, weekly"
    assert limits["3p_weekly"].percent == 20.0
    assert limits["3p_weekly"].resets_at == "2026-11-24T23:59:31Z"
    assert limits["3p_5h"].label == "Claude and GPT models, 5-hour"
    assert limits["3p_5h"].percent == 50.0
    assert limits["gemini"].label == "Gemini models, 5-hour"
    assert limits["gemini"].percent == 10.0


def _five_hour_readings(rows: tuple[tuple[str, float, str], ...]) -> str:
    return "".join(
        json.dumps({"t": t, "status": 200, "source_key": "claude:acct", "raw": {"five_hour": {"utilization": used, "resets_at": resets}}}) + "\n"
        for t, used, resets in rows
    )


def test_a_reset_starts_every_chats_total_graph_and_log_over(tmp_path: Path) -> None:
    store, source = _store_with_one_credited_turn(tmp_path, LONG_PROMPT)
    store.readings_path.write_text(
        _five_hour_readings(
            (
                ("2026-11-17T09:59:00+00:00", 10, "2026-11-17T10:03:30+00:00"),
                ("2026-11-17T10:02:00+00:00", 30, "2026-11-17T10:03:30+00:00"),
                ("2026-11-17T10:04:00+00:00", 3, "2026-11-17T15:03:30+00:00"),
                ("2026-11-17T10:04:30+00:00", 4, "2026-11-17T15:03:30+00:00"),
            )
        )
    )
    attribution = store.attribute(source)
    # The 20 credited before the reset is gone; only what was used since counts.
    assert attribution.totals == {"chat-1": {"five_hour": 4.0}}
    assert attribution.series["chat-1"]["five_hour"] == [
        ("2026-11-17T10:04:00+00:00", 3.0),
        ("2026-11-17T10:04:30+00:00", 4.0),
    ]
    assert attribution.turns["chat-1"][0].credits == {"five_hour": 4.0}
    assert attribution.window_starts == {"five_hour": "2026-11-17T10:03:30+00:00"}


def test_an_unused_limit_whose_reset_time_keeps_moving_is_not_resetting(tmp_path: Path) -> None:
    store, source = _store_with_one_credited_turn(tmp_path, LONG_PROMPT)
    store.readings_path.write_text(
        _five_hour_readings(
            (
                ("2026-11-17T09:59:00+00:00", 0, "2026-11-17T14:59:00+00:00"),
                ("2026-11-17T10:02:00+00:00", 5, "2026-11-17T15:02:00+00:00"),
                ("2026-11-17T10:04:00+00:00", 6, "2026-11-17T15:04:00+00:00"),
            )
        )
    )
    attribution = store.attribute(source)
    assert attribution.totals == {"chat-1": {"five_hour": 6.0}}
    assert attribution.window_starts == {}


def test_a_stale_reading_from_before_a_reset_is_ignored(tmp_path: Path) -> None:
    store, source = _store_with_one_credited_turn(tmp_path, LONG_PROMPT)
    store.readings_path.write_text(
        _five_hour_readings(
            (
                ("2026-11-17T09:59:00+00:00", 5, "2026-11-17T12:00:00+00:00"),
                ("2026-11-17T10:01:00+00:00", 0, "2026-11-17T15:01:00+00:00"),
                # The AI answers once more with the old window's numbers.
                ("2026-11-17T10:02:00+00:00", 5, "2026-11-17T12:00:00+00:00"),
                ("2026-11-17T10:03:00+00:00", 1, "2026-11-17T15:01:00+00:00"),
            )
        )
    )
    attribution = store.attribute(source)
    assert attribution.window_starts == {"five_hour": "2026-11-17T10:01:00+00:00"}
    assert attribution.totals == {"chat-1": {"five_hour": 1.0}}


def test_a_limit_the_ai_starts_reporting_later_is_graphed_from_its_first_reading(tmp_path: Path) -> None:
    store, source = _store_with_one_credited_turn(tmp_path, LONG_PROMPT)
    rows = [
        {"t": "2026-11-17T09:59:00+00:00", "raw": {"five_hour": {"utilization": 1, "resets_at": "2026-11-17T15:00:00+00:00"}}},
        {
            "t": "2026-11-17T10:02:00+00:00",
            "raw": {
                "five_hour": {"utilization": 1, "resets_at": "2026-11-17T15:00:00+00:00"},
                "seven_day": {"utilization": 3, "resets_at": "2026-11-20T15:00:00+00:00"},
            },
        },
    ]
    store.readings_path.write_text(
        "".join(json.dumps({**row, "status": 200, "source_key": "claude:acct"}) + "\n" for row in rows)
    )
    assert store.attribute(source).first_read == {"seven_day": "2026-11-17T10:02:00+00:00"}


def test_a_reset_keeps_the_finished_window_to_look_back_at(tmp_path: Path) -> None:
    store, source = _store_with_one_credited_turn(tmp_path, LONG_PROMPT)
    store.readings_path.write_text(
        _five_hour_readings(
            (
                ("2026-11-17T09:59:00+00:00", 10, "2026-11-17T10:03:30+00:00"),
                ("2026-11-17T10:02:00+00:00", 30, "2026-11-17T10:03:30+00:00"),
                ("2026-11-17T10:04:00+00:00", 3, "2026-11-17T15:03:30+00:00"),
            )
        )
    )
    (window,) = store.attribute(source).past_windows["five_hour"]
    assert window.started_at == "2026-11-17T09:59:00+00:00"
    assert window.ended_at == "2026-11-17T10:03:30+00:00"
    assert window.level == 30.0
    assert window.totals == {"chat-1": 20.0}
    assert window.series == {"chat-1": [("2026-11-17T10:02:00+00:00", 20.0)]}
    assert [turn.credit for turn in window.turns] == [20.0]

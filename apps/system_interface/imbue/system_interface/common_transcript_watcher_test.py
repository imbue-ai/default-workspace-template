import pytest

from imbue.system_interface.common_transcript_auth_patterns import is_auth_error_text
from imbue.system_interface.common_transcript_watcher import _find_common_transcript_source
from imbue.system_interface.common_transcript_watcher import _map_tool_call
from imbue.system_interface.common_transcript_watcher import _map_usage
from imbue.system_interface.common_transcript_watcher import _to_frontend_event
from imbue.mngr.api.events import EventSourceInfo


def test_to_frontend_event_user_message() -> None:
    data = {
        "type": "user_message",
        "timestamp": "2026-01-01T00:00:00Z",
        "event_id": "evt-1",
        "source": "codex/common_transcript",
        "role": "user",
        "content": "hello",
    }
    assert _to_frontend_event(data) == {
        "timestamp": "2026-01-01T00:00:00Z",
        "event_id": "evt-1",
        "source": "codex/common_transcript",
        "type": "user_message",
        "role": "user",
        "content": "hello",
    }


def test_to_frontend_event_assistant_message_maps_tool_calls_and_usage() -> None:
    data = {
        "type": "assistant_message",
        "timestamp": "2026-01-01T00:00:00Z",
        "event_id": "evt-2",
        "source": "codex/common_transcript",
        "text": "doing the thing",
        "model": "gpt-5.5",
        "finish_reason": "stop",
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "tool_calls": [{"tool_call_id": "call-1", "tool_name": "Bash", "input_preview": "echo hi"}],
    }
    event = _to_frontend_event(data)
    assert event is not None
    assert event["type"] == "assistant_message"
    assert event["stop_reason"] == "stop"
    assert event["usage"] == {
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
    }
    assert event["tool_calls"] == [{"tool_call_id": "call-1", "tool_name": "Bash", "input_preview": "echo hi"}]
    assert event["is_auth_error"] is False


def test_to_frontend_event_assistant_message_missing_model_defaults_to_unknown() -> None:
    data = {
        "type": "assistant_message",
        "timestamp": "2026-01-01T00:00:00Z",
        "event_id": "evt-2b",
        "source": "codex/common_transcript",
        "text": "",
        "model": None,
        "usage": None,
    }
    event = _to_frontend_event(data)
    assert event is not None
    assert event["model"] == "unknown"
    assert event["usage"] is None


def test_to_frontend_event_assistant_message_detects_harness_auth_error() -> None:
    data = {
        "type": "assistant_message",
        "timestamp": "2026-01-01T00:00:00Z",
        "event_id": "evt-3",
        "source": "codex/common_transcript",
        "text": "ERROR: Your access token could not be refreshed because your refresh token was already used.",
        "model": "gpt-5.5",
    }
    event = _to_frontend_event(data)
    assert event is not None
    assert event["is_auth_error"] is True


def test_to_frontend_event_tool_result() -> None:
    data = {
        "type": "tool_result",
        "timestamp": "2026-01-01T00:00:00Z",
        "event_id": "evt-4",
        "source": "opencode/common_transcript",
        "tool_call_id": "call-1",
        "tool_name": "Bash",
        "output": "done",
        "is_error": True,
    }
    event = _to_frontend_event(data)
    assert event == {
        "timestamp": "2026-01-01T00:00:00Z",
        "event_id": "evt-4",
        "source": "opencode/common_transcript",
        "type": "tool_result",
        "tool_call_id": "call-1",
        "tool_name": "Bash",
        "output": "done",
        "is_error": True,
    }


def test_to_frontend_event_unknown_type_returns_none() -> None:
    assert _to_frontend_event({"type": "some_future_record_type"}) is None


def test_map_usage_passes_through_none() -> None:
    assert _map_usage(None) is None


def test_map_usage_normalizes_missing_cache_fields() -> None:
    assert _map_usage({"input_tokens": 1, "output_tokens": 2}) == {
        "input_tokens": 1,
        "output_tokens": 2,
        "cache_read_tokens": None,
        "cache_write_tokens": None,
    }


def test_map_tool_call_defaults_missing_fields() -> None:
    assert _map_tool_call({}) == {"tool_call_id": "", "tool_name": "", "input_preview": ""}


@pytest.mark.parametrize(
    ("sources", "expected_path"),
    [
        ([EventSourceInfo(source_path="codex/common_transcript", rotated_files=())], "codex/common_transcript"),
        ([EventSourceInfo(source_path="common_transcript", rotated_files=())], "common_transcript"),
    ],
)
def test_find_common_transcript_source_matches(sources: list[EventSourceInfo], expected_path: str) -> None:
    found = _find_common_transcript_source(sources)
    assert found is not None
    assert found.source_path == expected_path


def test_find_common_transcript_source_excludes_logs_and_unrelated() -> None:
    sources = [
        EventSourceInfo(source_path="logs/common_transcript", rotated_files=()),
        EventSourceInfo(source_path="usage", rotated_files=()),
    ]
    assert _find_common_transcript_source(sources) is None


def test_is_auth_error_text_matches_known_codex_pattern() -> None:
    assert is_auth_error_text("codex", "token_expired: please sign in again")
    assert is_auth_error_text("codex", "got a 401 Unauthorized response")


def test_is_auth_error_text_no_pattern_for_unseeded_harness() -> None:
    # antigravity/opencode have no live-confirmed patterns yet (see module
    # docstring) -- must not false-positive on generic text.
    assert not is_auth_error_text("antigravity", "token_expired")
    assert not is_auth_error_text("opencode", "401 Unauthorized")


def test_is_auth_error_text_empty_text_is_false() -> None:
    assert not is_auth_error_text("codex", "")

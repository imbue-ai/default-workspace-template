"""Tests for ``lookup_user.py``: reading the look-up's answer, and what it files with the chat app and tells the agent.

The web search and the chat app are stood in for; the search's own flags are pinned in the use-ai-integration
skill's ``claude_p_test.py``.

Run via: ``uv run pytest .agents/shared/onboarding-agent/scripts/lookup_user_test.py``
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parent / "lookup_user.py"
_spec = importlib.util.spec_from_file_location("lookup_user", _SCRIPT)
assert _spec is not None and _spec.loader is not None
lookup_user = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = lookup_user
_spec.loader.exec_module(lookup_user)

_CHAT = "agent-00000000000000000000000000000001"
_ADA = {
    "name": "Ada Lovelace",
    "summary": "Mathematician working on the Analytical Engine",
    "source_url": "https://example.org/ada",
    "source_title": "Ada Lovelace - Example",
}


class _Answer:
    def __init__(self, status: int, body: object) -> None:
        self.status = status
        self.body = body


class _FakeMessageChat:
    """The two message_chat.py functions the script uses, recording each POST."""

    class ChatAppUnreachableError(Exception):
        pass

    def __init__(
        self, statuses: list[int] | None = None, is_unreachable: bool = False
    ) -> None:
        self.statuses = statuses or [201]
        self.is_unreachable = is_unreachable
        self.posts: list[tuple[str, dict]] = []

    def chat_app_url(self, environ: object, cwd: Path) -> str:
        if self.is_unreachable:
            raise self.ChatAppUnreachableError("the chat app is not running")
        return "http://127.0.0.1:8010"

    def post_json(self, base_url: str, path: str, body: dict) -> _Answer:
        self.posts.append((path, body))
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return _Answer(
            status, {"detail": "nope"} if status >= 400 else {"match_id": "identity-1"}
        )


def _answer(*candidates: dict) -> str:
    return json.dumps({"candidates": list(candidates)})


def _run(
    search_answer: str | Exception,
    message_chat: _FakeMessageChat,
    environ: dict[str, str] | None = None,
) -> tuple[int, list[tuple[str, str, str]]]:
    searches: list[tuple[str, str, str]] = []

    def search(name: str, detail: str, model: str) -> str:
        searches.append((name, detail, model))
        if isinstance(search_answer, Exception):
            raise search_answer
        return search_answer

    code = lookup_user.main(
        ["--name", " Ada Lovelace ", "--detail", "London"],
        environ={"MINDS_CHAT_ID": _CHAT} if environ is None else environ,
        search=search,
        message_chat=message_chat,
        clock=iter(range(1000)).__next__,
        sleep=lambda _seconds: None,
    )
    return code, searches


def test_found_people_are_filed_as_a_popup_and_the_agent_is_told_to_wait_for_the_answer(
    capsys: pytest.CaptureFixture[str],
) -> None:
    message_chat = _FakeMessageChat()

    code, searches = _run(_answer(_ADA), message_chat)

    assert code == 0
    assert searches == [("Ada Lovelace", "London", lookup_user.LOOKUP_MODEL)]
    [(path, body)] = message_chat.posts
    assert path == "/api/identity-matches"
    assert body == {
        "chat_id": _CHAT,
        "searched_for": "Ada Lovelace, London",
        "candidates": [_ADA],
    }
    out = capsys.readouterr().out
    assert "showed the user 1 possible match(es)" in out
    assert "use nothing from the search before it" in out
    # What the pages said never reaches the agent through this notice, only how many were shown
    # and what the user typed.
    assert _ADA["summary"] not in out


def test_no_one_found_shows_no_popup_and_says_so(
    capsys: pytest.CaptureFixture[str],
) -> None:
    message_chat = _FakeMessageChat()

    code, _ = _run(_answer(), message_chat)

    assert code == 0
    assert message_chat.posts == []
    assert "found no one on the web who plausibly matches" in capsys.readouterr().out


def test_a_search_that_cannot_run_fails_with_its_reason(
    capsys: pytest.CaptureFixture[str],
) -> None:
    message_chat = _FakeMessageChat()

    code, _ = _run(
        lookup_user.LookupFailedError("claude -p exited 1: not signed in"), message_chat
    )

    assert code == 1
    assert message_chat.posts == []
    assert "could not run: claude -p exited 1: not signed in" in capsys.readouterr().out


def test_an_answer_that_is_not_the_expected_json_fails(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, _ = _run("I could not find anyone, sorry.", _FakeMessageChat())

    assert code == 1
    assert "gave no usable answer" in capsys.readouterr().out


def test_a_not_ready_chat_app_is_retried(capsys: pytest.CaptureFixture[str]) -> None:
    message_chat = _FakeMessageChat(statuses=[503, 503, 201])

    code, _ = _run(_answer(_ADA), message_chat)

    assert code == 0
    assert len(message_chat.posts) == 3


def test_a_refusal_or_an_unreachable_chat_app_fails(
    capsys: pytest.CaptureFixture[str],
) -> None:
    refused, _ = _run(_answer(_ADA), _FakeMessageChat(statuses=[404]))
    assert refused == 1
    assert "refused the popup (HTTP 404): nope" in capsys.readouterr().out

    unreachable, _ = _run(_answer(_ADA), _FakeMessageChat(is_unreachable=True))
    assert unreachable == 1
    assert "the popup could not be shown" in capsys.readouterr().out


def test_without_a_chat_id_it_refuses_before_searching(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, searches = _run(_answer(_ADA), _FakeMessageChat(), environ={})

    assert code == 1
    assert searches == []


def test_parse_reads_the_json_inside_other_text_and_keeps_the_first_three() -> None:
    four = [{**_ADA, "name": f"Ada {i}"} for i in range(4)]
    text = "Here is what I found:\n```json\n" + _answer(*four) + "\n```"

    parsed = lookup_user.parse_candidates(text)

    assert [candidate["name"] for candidate in parsed] == ["Ada 0", "Ada 1", "Ada 2"]


@pytest.mark.parametrize(
    "bad",
    [
        {**_ADA, "source_url": "javascript:alert(1)"},
        {**_ADA, "source_url": "example.org/ada"},
        {**_ADA, "name": "  "},
        {**_ADA, "summary": ""},
    ],
)
def test_parse_drops_a_candidate_without_a_web_page_or_with_a_blank_field(
    bad: dict,
) -> None:
    assert lookup_user.parse_candidates(_answer(bad, _ADA)) == [_ADA]


def test_parse_cuts_long_text_to_the_chat_apps_limits() -> None:
    long = {**_ADA, "summary": "word " * 200, "name": "  Ada   Lovelace  "}

    [parsed] = lookup_user.parse_candidates(_answer(long))

    assert parsed["name"] == "Ada Lovelace"
    assert len(parsed["summary"]) <= lookup_user.MAX_SUMMARY_LENGTH
    assert parsed["summary"].endswith("…")


def _stub_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> Path:
    record = tmp_path / "argv.json"
    stub = tmp_path / "bin" / "claude"
    stub.parent.mkdir()
    stub.write_text(
        f"#!/usr/bin/env python3\nimport json, sys\njson.dump(sys.argv[1:], open({str(record)!r}, 'w'))\n{body}"
    )
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{stub.parent}:{__import__('os').environ['PATH']}")
    return record


def test_search_web_runs_the_web_only_look_up_and_returns_its_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": _answer(_ADA),
        "total_cost_usd": 0.0,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    record = _stub_claude(tmp_path, monkeypatch, f"print(json.dumps({result!r}))\n")

    text = lookup_user.search_web("Ada Lovelace", "London", "claude-haiku-4-5")

    argv = json.loads(record.read_text())
    assert lookup_user.parse_candidates(text) == [_ADA]
    assert argv[argv.index("--tools") + 1] == "WebSearch,WebFetch"
    assert argv[-1] == "Find this person: Ada Lovelace, London."


def test_search_web_turns_a_failed_run_into_a_lookup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_claude(
        tmp_path, monkeypatch, "sys.stderr.write('not signed in')\nsys.exit(1)\n"
    )

    with pytest.raises(lookup_user.LookupFailedError, match="not signed in"):
        lookup_user.search_web("Ada Lovelace", "London", "claude-haiku-4-5")


def test_parse_refuses_an_answer_with_no_json_object() -> None:
    with pytest.raises(ValueError):
        lookup_user.parse_candidates("no json here")

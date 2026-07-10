"""Unit tests for pr_review.ask (per-line "ask an agent" investigator).

These never launch a real agent: the launcher is injected, and state is asserted
through the on-disk records. ``DATA_DIR`` defaults to a cwd-relative path, so
``monkeypatch.chdir(tmp_path)`` isolates each test's question store.
"""

from pathlib import Path

import pytest

from pr_review import ask
from pr_review.github import RepoTree

_REPO = "octocat/hello"
_SHA = "abc1234"


def _tree() -> RepoTree:
    return RepoTree(repo=_REPO, sha=_SHA, root=Path("/does/not/matter"))


def test_no_questions_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert ask.list_questions(_REPO, 1) == []


def test_create_question_records_running_and_invokes_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    launched: list[RepoTree] = []
    rec = ask.create_question(
        _tree(), _REPO, 1, path="main.py", line=3, side="RIGHT",
        question="what does this do?", launcher=launched.append,
    )
    assert rec["state"] == "running"
    assert rec["path"] == "main.py"
    assert rec["line"] == 3
    assert rec["question"] == "what does this do?"
    assert rec["head_sha"] == _SHA
    assert rec["log_tail"] == ""
    assert launched == [_tree()]
    listed = ask.list_questions(_REPO, 1)
    assert [r["id"] for r in listed] == [rec["id"]]


def test_create_question_normalizes_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    rec = ask.create_question(
        _tree(), _REPO, 1, path="a.py", line=1, side="RIGHT",
        question="q", model="nonsense", launcher=lambda _t: None,
    )
    assert rec["model"] == ask.DEFAULT_MODEL
    rec2 = ask.create_question(
        _tree(), _REPO, 1, path="a.py", line=1, side="RIGHT",
        question="q", model="claude-opus-4-8", launcher=lambda _t: None,
    )
    assert rec2["model"] == "claude-opus-4-8"


def test_question_status_includes_log_tail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    rec = ask.create_question(
        _tree(), _REPO, 2, path="a.py", line=1, side="RIGHT",
        question="q", launcher=lambda _t: None,
    )
    ask._log_path(_REPO, 2, rec["id"]).write_text("● Investigating: q\n$ grep foo\n")
    status = ask.question_status(_REPO, 2, rec["id"])
    assert status["state"] == "running"
    assert "grep foo" in status["log_tail"]


def test_question_status_missing_returns_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert ask.question_status(_REPO, 1, "deadbeef0000") is None


def test_unsafe_qid_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    # A path-traversal id must not escape the questions dir.
    assert ask.question_status(_REPO, 1, "../../etc") is None
    assert ask._safe_qid("../evil") is False
    assert ask._safe_qid("abc123def456") is True


def test_delete_question_removes_record_and_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    rec = ask.create_question(
        _tree(), _REPO, 3, path="a.py", line=1, side="RIGHT",
        question="q", launcher=lambda _t: None,
    )
    ask._log_path(_REPO, 3, rec["id"]).write_text("log\n")
    result = ask.delete_question(_REPO, 3, rec["id"])
    assert result == {"ok": True, "id": rec["id"]}
    assert ask.list_questions(_REPO, 3) == []
    assert not ask._record_path(_REPO, 3, rec["id"]).exists()
    assert not ask._log_path(_REPO, 3, rec["id"]).exists()


def test_build_prompt_mentions_file_line_side_and_checkout() -> None:
    prompt = ask._build_prompt(
        {"path": "src/x.py", "line": 42, "side": "LEFT", "question": "why?"},
        "/tmp/checkout/repo-abc",
    )
    assert "src/x.py" in prompt
    assert "42" in prompt
    assert "removed (old) side" in prompt
    assert "why?" in prompt
    assert "/tmp/checkout/repo-abc" in prompt

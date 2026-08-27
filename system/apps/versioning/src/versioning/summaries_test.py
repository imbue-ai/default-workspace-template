from pathlib import Path

import pytest

from versioning.data_types import SummaryGenerationError
from versioning.data_types import VersionSummary
from versioning.summaries import _parse_summary_response
from versioning.summaries import generate_and_cache_summary
from versioning.summaries import read_cached_summary


def test_parse_summary_response_reads_the_json_object() -> None:
    summary = _parse_summary_response("a" * 40, 'Sure! {"title": "New look", "description": "The page got a calmer design."}')
    assert summary.title == "New look"
    assert summary.description == "The page got a calmer design."


def test_parse_summary_response_rejects_missing_fields() -> None:
    with pytest.raises(SummaryGenerationError):
        _parse_summary_response("a" * 40, '{"title": ""}')
    with pytest.raises(SummaryGenerationError):
        _parse_summary_response("a" * 40, "no json here")
    with pytest.raises(SummaryGenerationError):
        _parse_summary_response("a" * 40, '{"title": {broken}')


def test_read_cached_summary_roundtrip_and_missing(tmp_path: Path) -> None:
    cache_dir = tmp_path / "summaries"
    assert read_cached_summary(cache_dir, "a" * 40) is None
    cache_dir.mkdir()
    summary = VersionSummary(sha="a" * 40, title="First build", description="The app appeared.")
    (cache_dir / f"{'a' * 40}.json").write_text(summary.model_dump_json())
    assert read_cached_summary(cache_dir, "a" * 40) == summary


def test_read_cached_summary_discards_unreadable_file(tmp_path: Path) -> None:
    cache_dir = tmp_path / "summaries"
    cache_dir.mkdir()
    (cache_dir / f"{'b' * 40}.json").write_text("not json")
    assert read_cached_summary(cache_dir, "b" * 40) is None


def test_generate_and_cache_summary_returns_cached_without_a_model_call(tmp_path: Path) -> None:
    cache_dir = tmp_path / "summaries"
    cache_dir.mkdir()
    cached = VersionSummary(sha="c" * 40, title="Cached", description="Already written.")
    (cache_dir / f"{'c' * 40}.json").write_text(cached.model_dump_json())
    # A model call would explode without credentials; the cache short-circuits it.
    result = generate_and_cache_summary(cache_dir, "c" * 40, "ignored", "message", "diff")
    assert result == cached


def test_generate_and_cache_summary_wraps_model_call_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A failed model call must surface as SummaryGenerationError (502), not an unhandled 500.
    def _boom(*args: object, **kwargs: object) -> object:
        raise OSError("claude binary missing")

    monkeypatch.setattr("versioning.summaries.claude_p_completion", _boom)
    with pytest.raises(SummaryGenerationError, match="Summary model call failed"):
        generate_and_cache_summary(tmp_path / "summaries", "d" * 40, None, "message", "diff")

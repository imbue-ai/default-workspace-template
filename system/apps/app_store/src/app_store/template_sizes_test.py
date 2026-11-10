from pathlib import Path

import httpx
import pytest

from app_store import template_sizes
from app_store.template_sizes import GithubRepositorySizeFetcher
from app_store.template_sizes import TemplateSizeStore
from app_store.template_sizes import github_api_url
from app_store.testing import FakeRepositorySizeFetcher

_API = "https://api.github.com/repos/someone/thing"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/someone/thing", _API),
        ("https://github.com/someone/thing.git", _API),
        ("https://github.com/someone/thing/", _API),
        ("https://gitlab.com/someone/thing", None),
        ("https://github.com/someone", None),
    ],
)
def test_only_github_repositories_have_an_api_url(url: str, expected: str | None) -> None:
    assert github_api_url(url) == expected


def test_a_size_is_looked_up_once_and_cached_on_disk(tmp_path: Path) -> None:
    fetcher = FakeRepositorySizeFetcher(size_by_url={_API: 512})
    cache = tmp_path / "sizes.json"
    store = TemplateSizeStore(cache_path=cache, fetcher=fetcher)
    assert store.size_kb("https://github.com/someone/thing") == 512
    assert store.size_kb("https://github.com/someone/thing") == 512
    assert TemplateSizeStore(cache_path=cache, fetcher=fetcher).size_kb("https://github.com/someone/thing") == 512
    assert fetcher.fetched == [_API]


def test_a_failed_lookup_is_remembered_and_not_retried_at_once(tmp_path: Path) -> None:
    fetcher = FakeRepositorySizeFetcher()
    store = TemplateSizeStore(cache_path=tmp_path / "sizes.json", fetcher=fetcher)
    assert store.size_kb("https://github.com/someone/thing") is None
    assert store.size_kb("https://github.com/someone/thing") is None
    assert fetcher.fetched == [_API]
    assert store.size_kb("https://example.com/not-github") is None


def test_an_unreadable_cache_is_ignored(tmp_path: Path) -> None:
    cache = tmp_path / "sizes.json"
    cache.write_text('{"sizes": {"x": {"size_kb": "big"}}}')
    fetcher = FakeRepositorySizeFetcher(size_by_url={_API: 7})
    assert TemplateSizeStore(cache_path=cache, fetcher=fetcher).size_kb("https://github.com/someone/thing") == 7


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (httpx.Response(200, json={"size": 2048, "name": "thing"}), 2048),
        (httpx.Response(200, json=[{"size": 2048}]), None),
        (httpx.Response(200, json={"size": "big"}), None),
        (httpx.Response(200, text="<html>rate limited</html>"), None),
        (httpx.Response(403, json={"message": "API rate limit exceeded"}), None),
    ],
    ids=["size", "not-an-object", "not-a-number", "not-json", "refused"],
)
def test_the_github_fetcher_reads_the_size_and_anything_else_is_not_estimated(
    monkeypatch: pytest.MonkeyPatch, answer: httpx.Response, expected: int | None
) -> None:
    answer.request = httpx.Request("GET", _API)
    monkeypatch.setattr(template_sizes.httpx, "get", lambda url, **kwargs: answer)
    assert GithubRepositorySizeFetcher().fetch_size_kb(_API) == expected


def test_the_github_fetcher_reads_no_network_as_not_estimated(monkeypatch: pytest.MonkeyPatch) -> None:
    def _unreachable(url: str, **kwargs: object) -> httpx.Response:
        raise httpx.ConnectError("no network")

    monkeypatch.setattr(template_sizes.httpx, "get", _unreachable)
    assert GithubRepositorySizeFetcher().fetch_size_kb(_API) is None

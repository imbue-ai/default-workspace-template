import pytest

from memories.request_guard import is_write_allowed


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS", "get"])
def test_reads_are_always_allowed(method: str) -> None:
    assert is_write_allowed(method, "cross-site", None) is True


@pytest.mark.parametrize("fetch_site", ["same-origin", "Same-Origin", " same-origin ", None])
def test_a_json_write_from_this_page_or_from_outside_a_browser_is_allowed(fetch_site: str | None) -> None:
    assert is_write_allowed("DELETE", fetch_site, "application/json") is True
    assert is_write_allowed("PUT", fetch_site, "application/json; charset=utf-8") is True


@pytest.mark.parametrize("fetch_site", ["same-site", "cross-site", "none", ""])
def test_a_write_from_any_other_page_is_refused(fetch_site: str) -> None:
    assert is_write_allowed("DELETE", fetch_site, "application/json") is False


@pytest.mark.parametrize(
    "content_type", [None, "text/plain", "application/x-www-form-urlencoded", "multipart/form-data"]
)
def test_a_write_that_is_not_json_is_refused_even_from_this_page(content_type: str | None) -> None:
    assert is_write_allowed("PUT", "same-origin", content_type) is False
    assert is_write_allowed("PUT", None, content_type) is False

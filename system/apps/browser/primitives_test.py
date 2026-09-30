import pytest
from browser.errors import InvalidBrowserNameValueError, InvalidStartUrlError
from browser.primitives import (
    AbsoluteHttpUrl,
    BrowserName,
    LocalHttpUrl,
    browser_page_path,
)


@pytest.mark.parametrize("value", ["browser-1", "alex-smith", "research-2b"])
def test_browser_name_accepts_the_daemons_names(value: str) -> None:
    assert BrowserName(value) == value


@pytest.mark.parametrize(
    "value", ["", "Browser-1", "0", "a--b", "-lead", "a.b", "x" * 41]
)
def test_browser_name_rejects_what_the_daemon_rejects(value: str) -> None:
    with pytest.raises(InvalidBrowserNameValueError, match="invalid browser name"):
        BrowserName(value)


def test_the_browser_page_selects_the_browser_by_session() -> None:
    assert browser_page_path(BrowserName("browser-3")) == "/?session=browser-3"


@pytest.mark.parametrize("value", ["https://example.com", "http://localhost:8080/path?q=1"])
def test_a_start_url_is_an_absolute_http_url(value: str) -> None:
    assert AbsoluteHttpUrl(value) == value


@pytest.mark.parametrize(
    ("value", "problem"),
    [
        ("ftp://example.com", "expected an absolute http or https URL"),
        ("/docs/", "expected an absolute http or https URL"),
        ("https://", "expected an absolute http or https URL"),
        ("https://exa mple.com", "whitespace and control characters"),
        ("https://example.com/" + "a" * 2048, "over the 2048-character limit"),
    ],
)
def test_a_start_url_that_is_not_absolute_http_is_refused(value: str, problem: str) -> None:
    with pytest.raises(InvalidStartUrlError, match=problem):
        AbsoluteHttpUrl(value)


@pytest.mark.parametrize(
    "value",
    [
        "http://localhost:3000/app?x=1",
        "HTTP://LOCALHOST:8000/",
        "http://127.0.0.1:8080/x",
        "http://[::1]:5000/app",
        "https://dev.localhost:5173/",
        "http://a.b.localhost/",
        "http://user:pass@localhost:8000/",
    ],
)
def test_a_link_for_the_human_may_name_any_local_host(value: str) -> None:
    assert LocalHttpUrl(value) == value


@pytest.mark.parametrize(
    ("value", "problem"),
    [
        ("https://example.com/", "only an address on this machine"),
        ("http://localhost.example.com/", "only an address on this machine"),
        ("http://localhost@evil.example/", "only an address on this machine"),
        ("http://127.0.0.2:8000/", "only an address on this machine"),
        ("ftp://localhost/", "expected an absolute http or https URL"),
    ],
)
def test_a_link_for_the_human_off_this_machine_is_refused(value: str, problem: str) -> None:
    with pytest.raises(InvalidStartUrlError, match=problem):
        LocalHttpUrl(value)

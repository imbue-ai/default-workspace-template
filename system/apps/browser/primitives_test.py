import pytest
from browser.errors import InvalidBrowserNameValueError
from browser.primitives import (
    BrowserName,
    derive_browser_title,
    instance_url_for_browser,
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


def test_numbered_names_derive_their_title_and_legacy_names_are_verbatim() -> None:
    assert derive_browser_title(BrowserName("browser-3")) == "Browser 3"
    assert derive_browser_title(BrowserName("browser-12")) == "Browser 12"
    assert derive_browser_title(BrowserName("alex-smith")) == "alex-smith"
    assert derive_browser_title(BrowserName("browser-x")) == "browser-x"


def test_instance_url_selects_the_browser_by_session() -> None:
    assert instance_url_for_browser(BrowserName("browser-3")) == "/?session=browser-3"

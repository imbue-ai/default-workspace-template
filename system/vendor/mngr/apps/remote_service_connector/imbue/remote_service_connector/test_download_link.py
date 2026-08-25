"""The download link against the real stable feed.

The unit tests inject ``fetch``, so nothing there sees how the request is
actually made -- which is how a bare ``urlopen`` shipped: the feed's CDN answers
403 to ``Python-urllib/<version>`` by name, the resolver fails open, and every
download would have quietly served the fallback forever. This is the check that
observes that, so it has to reach the network.
"""

import pytest

from imbue.remote_service_connector.accounts_web import _DEFAULT_TARGET_BY_PLATFORM
from imbue.remote_service_connector.accounts_web import stable_mac_arm64_url
from imbue.remote_service_connector.testing import _make_accounts_web_test_client
from imbue.remote_service_connector.testing import clear_stable_download_link


@pytest.mark.release
def test_the_live_stable_feed_resolves_to_a_real_arm64_dmg() -> None:
    # The autouse fixture holds "could not be read" so the unit tests stay off
    # the feed; this one is here to reach it.
    clear_stable_download_link()

    resolved = stable_mac_arm64_url()

    assert resolved is not None, (
        "the stable channel manifest could not be read -- if this is a 403, the request is "
        "missing its User-Agent and every download would silently serve the fallback"
    )
    assert resolved.endswith("-arm64.dmg")
    # Equal to the fallback would mean this proves nothing about following stable.
    assert resolved != _DEFAULT_TARGET_BY_PLATFORM["mac-arm64"]


@pytest.mark.release
def test_the_route_itself_serves_stable_rather_than_the_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole path, with nothing stubbed.

    The unit test for this resolves a fixture manifest, so it proves the route
    serves what was resolved -- not that resolving the real feed produces the
    real answer. Everything between the request and the manifest is exercised
    here: aliasing, the resolver, the fetch, the parse.
    """
    client, _st, _codes = _make_accounts_web_test_client(monkeypatch)
    clear_stable_download_link()

    response = client.get("/download?platform=mac", follow_redirects=False)

    assert response.status_code == 302
    location = response.headers["location"]
    assert location != _DEFAULT_TARGET_BY_PLATFORM["mac-arm64"], (
        "the route served the fallback -- resolution is not reaching the redirect"
    )
    assert location.endswith("-arm64.dmg")

"""The download link against the real stable feed.

The unit tests parse a manifest string, so nothing there sees how the request is
actually made -- which is how a bare ``urlopen`` shipped: the feed's CDN answers
403 to ``Python-urllib/<version>`` by name, the resolver fails open, and every
download would have quietly served the fallback forever. This is the check that
observes that, so it has to reach the network.
"""

import pytest

from imbue.remote_service_connector.accounts_web import _DEFAULT_TARGET_BY_PLATFORM
from imbue.remote_service_connector.accounts_web import _MAC_ARM64_PLATFORM
from imbue.remote_service_connector.accounts_web import stable_mac_arm64_url
from imbue.remote_service_connector.testing import _make_accounts_web_test_client
from imbue.remote_service_connector.testing import clear_stable_download_link
from imbue.remote_service_connector.testing import read_stable_download_link


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


@pytest.mark.release
def test_the_pinned_fallback_is_the_url_the_live_feed_names() -> None:
    """Typed by hand at every stable promotion, and served to everyone while the feed is down.

    The drift tests beside the constant check it against what the repo declares --
    the app id, the version, the build id. Only the feed can say the build was
    really published under the name they agree on.
    """
    clear_stable_download_link()

    resolved = stable_mac_arm64_url()

    assert resolved is not None, "the stable channel manifest could not be read, so this says nothing about the pin"
    assert resolved == _DEFAULT_TARGET_BY_PLATFORM[_MAC_ARM64_PLATFORM], (
        "the pinned download fallback is not the url stable serves -- bump it per the Release "
        "channels section of apps/minds/docs/deploy/ops/app-release.md, or wait for CI to publish the "
        "manifest if the promotion only just merged"
    )


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
    assert location.endswith("-arm64.dmg")
    # The fallback names the same build as stable, so comparing urls cannot tell
    # a resolved redirect from a fallback one. What the request left in the cache
    # can: nothing held means the route never asked the resolver.
    resolved = read_stable_download_link()
    assert resolved is not None, "the route served the fallback -- resolution is not reaching the redirect"
    assert location == resolved

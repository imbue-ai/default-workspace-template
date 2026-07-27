import httpx
import pytest

from imbue.apt_mirror.errors import AptMirrorError
from imbue.apt_mirror.errors import AptMirrorTransientUpstreamError
from imbue.apt_mirror.errors import AptMirrorUpstreamError
from imbue.apt_mirror.fetcher import HttpUpstreamFetcher


def _fetcher_answering(status_code: int, content: bytes = b"") -> HttpUpstreamFetcher:
    """A fetcher whose client answers every request with one canned status."""
    transport = httpx.MockTransport(lambda request: httpx.Response(status_code, content=content))
    return HttpUpstreamFetcher(client=httpx.Client(transport=transport))


def test_fetch_returns_body_on_success() -> None:
    fetcher = _fetcher_answering(200, b"deb-bytes")
    assert fetcher.fetch("https://deb.debian.org/debian/pool/x.deb") == b"deb-bytes"


def test_fetch_maps_404_to_none() -> None:
    fetcher = _fetcher_answering(404)
    assert fetcher.fetch("https://deb.debian.org/debian/pool/x.deb") is None


def test_fetch_raises_clean_mirror_error_on_unexpected_4xx_without_retrying() -> None:
    """A 403 is fatal on the first attempt (no retry sleeps) but still an AptMirrorError for clean CLI reporting."""
    fetcher = _fetcher_answering(403)
    with pytest.raises(AptMirrorUpstreamError) as exc_info:
        fetcher.fetch("https://deb.debian.org/debian/pool/x.deb")
    assert isinstance(exc_info.value, AptMirrorError)
    assert not isinstance(exc_info.value, AptMirrorTransientUpstreamError)
    assert "403" in str(exc_info.value)

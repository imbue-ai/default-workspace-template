import base64
from pathlib import Path
from types import SimpleNamespace

import httpx
import litellm_image
import pytest


def _links(content_by_url: dict[str, bytes]) -> httpx.Client:
    """A client that serves each link's content, and a 404 for any other."""

    def handle(request: httpx.Request) -> httpx.Response:
        content = content_by_url.get(str(request.url))
        return (
            httpx.Response(404)
            if content is None
            else httpx.Response(200, content=content)
        )

    return httpx.Client(transport=httpx.MockTransport(handle))


def test_an_inline_image_is_written_as_it_came(tmp_path: Path) -> None:
    items = [
        SimpleNamespace(b64_json=base64.b64encode(b"png-bytes").decode(), url=None)
    ]

    paths = litellm_image.write_candidates(items, tmp_path, _links({}))

    assert [path.name for path in paths] == ["candidate-0.png"]
    assert paths[0].read_bytes() == b"png-bytes"


def test_an_image_with_neither_data_nor_link_is_refused() -> None:
    with pytest.raises(
        litellm_image.ImageCandidateError, match="neither data nor a link"
    ):
        litellm_image.candidate_bytes(
            SimpleNamespace(b64_json=None, url=None), _links({})
        )


def test_a_linked_image_is_downloaded_and_a_dead_link_is_an_error() -> None:
    client = _links({"https://images.test/a.png": b"linked-png"})

    assert (
        litellm_image.candidate_bytes(
            SimpleNamespace(b64_json=None, url="https://images.test/a.png"), client
        )
        == b"linked-png"
    )
    with pytest.raises(httpx.HTTPStatusError):
        litellm_image.candidate_bytes(
            SimpleNamespace(b64_json=None, url="https://images.test/gone.png"), client
        )

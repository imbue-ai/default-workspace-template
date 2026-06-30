"""Unit tests for the image-path classification used by the chat image route."""

import pytest

from imbue.system_interface.image_serving import image_mime_type_for_path
from imbue.system_interface.image_serving import try_serve_image


@pytest.mark.parametrize(
    ("url_path", "expected_mime_type"),
    [
        ("mngr/code/runtime/chat-images/chart.png", "image/png"),
        ("a/b/photo.jpg", "image/jpeg"),
        ("a/b/photo.jpeg", "image/jpeg"),
        ("a/b/anim.gif", "image/gif"),
        ("a/b/pic.webp", "image/webp"),
        ("a/b/plot.svg", "image/svg+xml"),
        # Case-insensitive on the extension.
        ("a/b/SHOT.PNG", "image/png"),
        ("a/b/Plot.SvG", "image/svg+xml"),
    ],
)
def test_image_mime_type_for_image_paths(url_path: str, expected_mime_type: str) -> None:
    assert image_mime_type_for_path(url_path) == expected_mime_type


@pytest.mark.parametrize(
    "url_path",
    [
        "notes.txt",
        "report.pdf",
        "index.html",
        "agent/some-client-route",
        "no_extension",
        "archive.png.gz",
    ],
)
def test_non_image_paths_have_no_mime_type(url_path: str) -> None:
    assert image_mime_type_for_path(url_path) is None


def test_try_serve_image_returns_none_for_non_image_path() -> None:
    """Non-image paths yield None so the catch-all falls through to the app shell."""
    assert try_serve_image("agent/some-client-route") is None

import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest
from markdown_preview_server import MarkdownPreviewHandler, read_asset_dir
from render_markdown_preview import SOURCE_RECORD_NAME, write_preview


@pytest.fixture
def preview_server(tmp_path: Path):
    """A running preview server over a rendered README with a local image."""
    source_dir = tmp_path / "repo"
    source_dir.mkdir()
    (source_dir / "template.svg").write_text("<svg><rect/></svg>")
    (source_dir / "README.md").write_text(
        '<p align="center"><img src="template.svg"></p>\n\n# Demo\n'
    )
    state_dir = tmp_path / "state"
    write_preview(source_dir / "README.md", state_dir)

    MarkdownPreviewHandler.state_dir = state_dir
    server = ThreadingHTTPServer(("127.0.0.1", 0), MarkdownPreviewHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", state_dir, source_dir
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_the_root_serves_the_rendered_page(preview_server) -> None:
    base_url, _, _ = preview_server

    with urlopen(f"{base_url}/") as response:
        body = response.read().decode()

    assert response.status == 200
    assert "<h1>Demo</h1>" in body
    assert '<p align="center">' in body


def test_a_relative_image_resolves_from_the_source_directory(preview_server) -> None:
    # This is the check the whole service exists for: a README's relative image
    # path is right in the source tree but can 404 once published, and a preview
    # that could not serve it would hide exactly that class of bug.
    base_url, _, _ = preview_server

    with urlopen(f"{base_url}/template.svg") as response:
        body = response.read().decode()

    assert response.status == 200
    assert "<svg>" in body


def test_a_traversal_out_of_the_previewed_directory_is_refused(
    preview_server,
) -> None:
    base_url, _, _ = preview_server

    with pytest.raises(HTTPError) as excinfo:
        urlopen(f"{base_url}/%2e%2e%2f%2e%2e%2fetc%2fpasswd")

    assert excinfo.value.code == 403


def test_a_missing_asset_is_a_404(preview_server) -> None:
    base_url, _, _ = preview_server

    with pytest.raises(HTTPError) as excinfo:
        urlopen(f"{base_url}/no-such-image.png")

    assert excinfo.value.code == 404


def test_the_page_is_not_cached(preview_server) -> None:
    # The tab is refreshed in place after a re-render; a cached copy would show
    # the version the user already fixed.
    base_url, _, _ = preview_server

    with urlopen(f"{base_url}/") as response:
        assert response.headers["Cache-Control"] == "no-store"


def test_nothing_rendered_yet_serves_instructions_rather_than_failing(
    tmp_path: Path,
) -> None:
    # The service starts at boot, long before anyone renders anything. An empty
    # preview is a normal state and must not crash the serving loop.
    MarkdownPreviewHandler.state_dir = tmp_path / "empty"
    server = ThreadingHTTPServer(("127.0.0.1", 0), MarkdownPreviewHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urlopen(f"http://127.0.0.1:{server.server_address[1]}/") as response:
            body = response.read().decode()
        assert response.status == 200
        assert "Nothing rendered yet" in body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_an_unreadable_source_record_reads_as_nothing_rendered(tmp_path: Path) -> None:
    (tmp_path / SOURCE_RECORD_NAME).write_text("{not json")

    assert read_asset_dir(tmp_path) is None


def test_a_source_record_naming_a_vanished_directory_reads_as_nothing(
    tmp_path: Path,
) -> None:
    (tmp_path / SOURCE_RECORD_NAME).write_text(
        json.dumps({"asset_dir": str(tmp_path / "gone")})
    )

    assert read_asset_dir(tmp_path) is None

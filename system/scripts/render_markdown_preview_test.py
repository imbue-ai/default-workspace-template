import json
from pathlib import Path

from render_markdown_preview import (
    RENDERED_PAGE_NAME,
    SOURCE_RECORD_NAME,
    build_page,
    render_markdown,
    write_preview,
)


def test_raw_html_passes_through_untouched() -> None:
    # The whole reason a README preview exists: an inspiration's landing page is
    # a centered raw-HTML hero plus a badge, and a renderer that escapes those
    # shows the user something GitHub will never display.
    rendered = render_markdown(
        '<p align="center"><img alt="hero" src="inspiration.svg" width="480"></p>\n'
    )

    assert '<p align="center">' in rendered
    assert 'src="inspiration.svg"' in rendered
    assert "&lt;p align" not in rendered


def test_tables_render() -> None:
    # GitHub renders pipe tables; bare CommonMark does not. Catching that
    # difference is why the preset is js-default rather than commonmark.
    rendered = render_markdown("| a | b |\n|---|---|\n| 1 | 2 |\n")

    assert "<table>" in rendered
    assert "<td>1</td>" in rendered


def test_ordinary_markdown_still_renders() -> None:
    rendered = render_markdown("# Title\n\nSome **bold** text and `code`.\n")

    assert "<h1>Title</h1>" in rendered
    assert "<strong>bold</strong>" in rendered
    assert "<code>code</code>" in rendered


def test_the_page_shows_the_source_path_with_a_copy_button(tmp_path: Path) -> None:
    source = tmp_path / "README.md"

    page = build_page("# Hi\n", source)

    assert str(source) in page
    assert 'id="copy-path"' in page


def test_a_path_with_html_metacharacters_is_escaped(tmp_path: Path) -> None:
    # The path is interpolated into both text and an attribute; a directory
    # named with a quote must not be able to break out of either.
    source = tmp_path / 'we"ird' / "README.md"

    page = build_page("# Hi\n", source)

    assert 'we"ird/README.md"' not in page
    assert "we&quot;ird" in page


def test_write_preview_records_the_asset_directory(tmp_path: Path) -> None:
    # The recorded asset dir is what lets the server resolve a relative
    # inspiration.svg -- without it every local image in the preview breaks,
    # which is exactly the failure the preview is supposed to surface.
    source_dir = tmp_path / "repo"
    source_dir.mkdir()
    source = source_dir / "README.md"
    source.write_text('<img src="inspiration.svg">\n')
    state_dir = tmp_path / "state"

    page_path = write_preview(source, state_dir)

    assert page_path == state_dir / RENDERED_PAGE_NAME
    assert page_path.is_file()
    record = json.loads((state_dir / SOURCE_RECORD_NAME).read_text())
    assert record["asset_dir"] == str(source_dir)
    assert record["source_path"] == str(source)


def test_rendering_twice_replaces_the_previous_preview(tmp_path: Path) -> None:
    # The tab is refreshed in place after a fix; a stale page would show the
    # user the version they already corrected.
    state_dir = tmp_path / "state"
    first = tmp_path / "first.md"
    first.write_text("# First\n")
    second = tmp_path / "second.md"
    second.write_text("# Second\n")

    write_preview(first, state_dir)
    write_preview(second, state_dir)

    page = (state_dir / RENDERED_PAGE_NAME).read_text()
    assert "<h1>Second</h1>" in page
    assert "<h1>First</h1>" not in page

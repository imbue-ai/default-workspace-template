import json
from pathlib import Path

import pytest
from render_markdown_preview import (
    RENDERED_PAGE_NAME,
    SOURCE_RECORD_NAME,
    build_page,
    main,
    render_markdown,
    write_preview,
)


def test_raw_html_passes_through_untouched() -> None:
    # The whole reason a README preview exists: atemplate's landing page is
    # a centered raw-HTML hero plus a badge, and a renderer that escapes those
    # shows the user something GitHub will never display.
    rendered = render_markdown(
        '<p align="center"><img alt="hero" src="template.svg" width="480"></p>\n'
    )

    assert '<p align="center">' in rendered
    assert 'src="template.svg"' in rendered
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
    # template.svg -- without it every local image in the preview breaks,
    # which is exactly the failure the preview is supposed to surface.
    source_dir = tmp_path / "repo"
    source_dir.mkdir()
    source = source_dir / "README.md"
    source.write_text('<img src="template.svg">\n')
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


# --- the tab only exists while a preview does ---


def _recording_runner(calls: list[str], result: tuple[bool, str] = (True, "")):
    def run(action: str) -> tuple[bool, str]:
        calls.append(action)
        return result

    return run


def test_rendering_starts_the_preview_service(tmp_path: Path) -> None:
    """A tab appears because a render happened, not because the box booted.

    The service is not autostarted: a registered service is a panel in the
    user's workspace, and a previewer that is idle almost all the time would
    sit there empty forever.
    """
    calls: list[str] = []
    source = tmp_path / "README.md"
    source.write_text("# Hi\n")

    exit_code = main(
        [str(source), "--state-dir", str(tmp_path / "state")],
        run_supervisorctl=_recording_runner(calls),
    )

    assert exit_code == 0
    assert calls == ["start"]
    assert (tmp_path / "state" / RENDERED_PAGE_NAME).is_file()


def test_close_stops_the_service_and_renders_nothing(tmp_path: Path) -> None:
    # Stopping is what removes the tab: the server withdraws its port as it
    # exits, so the panel goes with it instead of lingering on a dead origin.
    calls: list[str] = []

    exit_code = main(
        ["--close", "--state-dir", str(tmp_path / "state")],
        run_supervisorctl=_recording_runner(calls),
    )

    assert exit_code == 0
    assert calls == ["stop"]
    assert not (tmp_path / "state" / RENDERED_PAGE_NAME).exists()


def test_a_render_still_succeeds_when_there_is_no_supervisord(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Outside a workspace container there is no supervisord, and the rendered
    # file is still useful -- so this reports the problem rather than failing
    # the render.
    source = tmp_path / "README.md"
    source.write_text("# Hi\n")

    exit_code = main(
        [str(source), "--state-dir", str(tmp_path / "state")],
        run_supervisorctl=_recording_runner([], (False, "supervisorctl not found")),
    )

    assert exit_code == 0
    assert (tmp_path / "state" / RENDERED_PAGE_NAME).is_file()
    assert "could not start the preview service" in capsys.readouterr().err


def test_no_arguments_is_a_usage_error() -> None:
    with pytest.raises(SystemExit):
        main([])


def test_the_default_state_dir_does_not_depend_on_the_callers_cwd() -> None:
    """The renderer is run from wherever the work is; the server reads one place.

    The publish flow renders an assembled README from the worker's worktree
    while the service serves the workspace's state dir. A cwd-relative default
    wrote the page next to the caller, so re-rendering silently did nothing and
    the tab kept showing a stale page -- which looks exactly like a render that
    worked.
    """
    from render_markdown_preview import PREVIEW_STATE_DIR

    assert PREVIEW_STATE_DIR.is_absolute()
    assert PREVIEW_STATE_DIR.parts[-3:] == ("data", ".state", "markdown-preview")

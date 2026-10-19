from pathlib import Path

from inline_snapshot import snapshot

from screen.xfce_helpers import APPLICATION_DESKTOP_RELATIVE_PATH
from screen.xfce_helpers import HELPER_DESKTOP_RELATIVE_PATH
from screen.xfce_helpers import HELPERS_RC_RELATIVE_PATH
from screen.xfce_helpers import MIMEAPPS_RELATIVE_PATH
from screen.xfce_helpers import MimeAppsParser
from screen.xfce_helpers import render_helper_desktop
from screen.xfce_helpers import render_helpers_rc
from screen.xfce_helpers import render_mimeapps_list
from screen.xfce_helpers import write_browser_helper_files


def test_the_helper_names_the_browser_command_for_the_web_browser_category() -> None:
    assert render_helper_desktop() == snapshot("""\
[Desktop Entry]
Version=1.0
Type=X-XFCE-Helper
Name=Browser (Screen)
Icon=web-browser
StartupNotify=false
X-XFCE-Binaries=screen-browser-open;
X-XFCE-Category=WebBrowser
X-XFCE-Commands=%B;
X-XFCE-CommandsWithParameter=%B "%s";
""")


def test_helpers_rc_sets_the_web_browser_and_keeps_the_other_helpers() -> None:
    assert render_helpers_rc(None) == "WebBrowser=imbue-screen-browser\n"
    merged = render_helpers_rc("TerminalEmulator=xfce4-terminal\nWebBrowser=firefox\n\nFileManager=Thunar\n")
    assert merged == snapshot("""\
TerminalEmulator=xfce4-terminal
FileManager=Thunar
WebBrowser=imbue-screen-browser
""")


def test_mimeapps_list_sets_the_url_schemes_and_keeps_the_other_associations() -> None:
    existing = "[Default Applications]\ntext/plain=mousepad.desktop;\n\n[Added Associations]\nimage/png=ristretto.desktop;\n"
    parser = MimeAppsParser()
    parser.read_string(render_mimeapps_list(existing))
    assert dict(parser["Default Applications"]) == {
        "text/plain": "mousepad.desktop;",
        "x-scheme-handler/http": "imbue-screen-browser.desktop;",
        "x-scheme-handler/https": "imbue-screen-browser.desktop;",
    }
    assert dict(parser["Added Associations"]) == {"image/png": "ristretto.desktop;"}
    fresh = MimeAppsParser()
    fresh.read_string(render_mimeapps_list(None))
    assert fresh.sections() == ["Default Applications"]


def test_the_four_entries_are_written_into_the_home_and_rewritten_only_when_changed(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / HELPERS_RC_RELATIVE_PATH).parent.mkdir(parents=True)
    (home / HELPERS_RC_RELATIVE_PATH).write_text("TerminalEmulator=xfce4-terminal\n")
    write_browser_helper_files(home)
    assert (home / HELPER_DESKTOP_RELATIVE_PATH).read_text() == render_helper_desktop()
    assert "Exec=screen-browser-open %u" in (home / APPLICATION_DESKTOP_RELATIVE_PATH).read_text()
    assert (home / HELPERS_RC_RELATIVE_PATH).read_text() == (
        "TerminalEmulator=xfce4-terminal\nWebBrowser=imbue-screen-browser\n"
    )
    assert "x-scheme-handler/https=imbue-screen-browser.desktop;" in (home / MIMEAPPS_RELATIVE_PATH).read_text()
    written_at = {path: path.stat().st_mtime_ns for path in home.rglob("*") if path.is_file()}
    write_browser_helper_files(home)
    assert {path: path.stat().st_mtime_ns for path in home.rglob("*") if path.is_file()} == written_at


def test_an_unwritable_home_is_left_alone(tmp_path: Path) -> None:
    not_a_directory = tmp_path / "file"
    not_a_directory.write_text("")
    write_browser_helper_files(not_a_directory)
    assert not_a_directory.read_text() == ""

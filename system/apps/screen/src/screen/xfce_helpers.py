"""The XFCE session's browser entries, rendered into the workspace home at each stack start (docs/system/specs/screen/
spec.md section 16): XFCE's "Web Browser" launcher and ``exo-open --launch WebBrowser`` resolve through
``helpers.rc`` to a helper, and GTK programs resolve an ``http(s)`` link through ``mimeapps.list`` to an application
entry. Both name the ``screen-browser-open`` command (the env.d unit installs it on PATH, and as the ``x-www-browser``
alternative), which is the Screen's own browser launch path, so every way of opening a browser on the Screen lands in
the fleet's Chromium rather than starting a second one."""

import configparser
import io
from pathlib import Path
from typing import Final

from loguru import logger

from imbue.imbue_common.pure import pure

HELPER_ID: Final[str] = "imbue-screen-browser"
BROWSER_OPEN_COMMAND: Final[str] = "screen-browser-open"
HELPERS_RC_RELATIVE_PATH: Final[Path] = Path(".config/xfce4/helpers.rc")
HELPER_DESKTOP_RELATIVE_PATH: Final[Path] = Path(".local/share/xfce4/helpers") / f"{HELPER_ID}.desktop"
APPLICATION_DESKTOP_RELATIVE_PATH: Final[Path] = Path(".local/share/applications") / f"{HELPER_ID}.desktop"
MIMEAPPS_RELATIVE_PATH: Final[Path] = Path(".config/mimeapps.list")
URL_SCHEME_MIME_TYPES: Final[tuple[str, ...]] = ("x-scheme-handler/http", "x-scheme-handler/https")

_HELPERS_RC_KEY: Final[str] = "WebBrowser"
_MIMEAPPS_DEFAULTS_SECTION: Final[str] = "Default Applications"


class MimeAppsParser(configparser.ConfigParser):
    """``mimeapps.list`` as an INI file whose keys (MIME types) keep their case."""

    def __init__(self) -> None:
        super().__init__(interpolation=None, delimiters=("=",))

    def optionxform(self, optionstr: str) -> str:
        return optionstr


@pure
def render_helper_desktop() -> str:
    """The XFCE helper entry: the category the launcher asks for, the command with and without a URL."""
    return (
        "[Desktop Entry]\n"
        "Version=1.0\n"
        "Type=X-XFCE-Helper\n"
        "Name=Browser (Screen)\n"
        "Icon=web-browser\n"
        "StartupNotify=false\n"
        f"X-XFCE-Binaries={BROWSER_OPEN_COMMAND};\n"
        "X-XFCE-Category=WebBrowser\n"
        "X-XFCE-Commands=%B;\n"
        'X-XFCE-CommandsWithParameter=%B "%s";\n'
    )


@pure
def render_application_desktop() -> str:
    """The application entry ``mimeapps.list`` points ``http(s)`` links at, also listed in XFCE's menu."""
    return (
        "[Desktop Entry]\n"
        "Version=1.0\n"
        "Type=Application\n"
        "Name=Browser\n"
        "Comment=Open a page in the Screen's browser\n"
        "Icon=web-browser\n"
        f"Exec={BROWSER_OPEN_COMMAND} %u\n"
        "Terminal=false\n"
        "StartupNotify=false\n"
        "Categories=Network;WebBrowser;\n"
        f"MimeType={';'.join(URL_SCHEME_MIME_TYPES)};\n"
    )


@pure
def render_helpers_rc(existing: str | None) -> str:
    """``helpers.rc`` with its ``WebBrowser`` set to the Screen's helper and every other line kept."""
    kept = [
        line
        for line in (existing or "").splitlines()
        if not line.strip().startswith(f"{_HELPERS_RC_KEY}=") and line.strip() != ""
    ]
    return "\n".join([*kept, f"{_HELPERS_RC_KEY}={HELPER_ID}"]) + "\n"


@pure
def render_mimeapps_list(existing: str | None) -> str:
    """``mimeapps.list`` with the ``http(s)`` scheme handlers set to the Screen's application entry and every other
    association kept."""
    parser = MimeAppsParser()
    if existing:
        parser.read_string(existing)
    if not parser.has_section(_MIMEAPPS_DEFAULTS_SECTION):
        parser.add_section(_MIMEAPPS_DEFAULTS_SECTION)
    for mime_type in URL_SCHEME_MIME_TYPES:
        parser.set(_MIMEAPPS_DEFAULTS_SECTION, mime_type, f"{HELPER_ID}.desktop;")
    rendered = io.StringIO()
    parser.write(rendered, space_around_delimiters=False)
    return rendered.getvalue()


def _read_if_present(path: Path) -> str | None:
    return path.read_text() if path.is_file() else None


def write_browser_helper_files(home: Path) -> None:
    """Render the four entries into ``home``, merging with what is there; a home that cannot be written is logged
    and left alone, since the Screen itself does not depend on them."""
    rendered = (
        (home / HELPER_DESKTOP_RELATIVE_PATH, render_helper_desktop()),
        (home / APPLICATION_DESKTOP_RELATIVE_PATH, render_application_desktop()),
        (home / HELPERS_RC_RELATIVE_PATH, render_helpers_rc(_read_if_present(home / HELPERS_RC_RELATIVE_PATH))),
        (home / MIMEAPPS_RELATIVE_PATH, render_mimeapps_list(_read_if_present(home / MIMEAPPS_RELATIVE_PATH))),
    )
    for path, text in rendered:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if _read_if_present(path) != text:
                path.write_text(text)
        except OSError as e:
            logger.warning("Could not write the Screen's browser entry {}: {}", path, e)

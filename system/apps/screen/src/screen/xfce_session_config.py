"""A desktop session without the XFCE panel (docs/system/specs/screen/spec.md section 4.3), for a Screen whose
environment turns the panel off (``SCREEN_DESKTOP_PANEL=off``, which an auth sandbox sets): its user signs in to one
service through one browser window, so a panel would only offer what the sandbox does not have.

With no saved session, ``xfce4-session`` starts the failsafe session its ``xfce4-session`` channel names. The
service writes the user's channel file (``~/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-session.xml``, which
xfconfd reads over the system one, property by property) before each stack start: it names a session of its own,
``ScreenNoPanel``, as the failsafe one, defines it as the window manager, the settings daemon and the desktop (the
system failsafe session's members without ``xfce4-panel`` and ``Thunar --daemon``), and turns ``SaveOnExit`` off so
no saved session ever takes its place. Every other property the file holds is kept. A session of its own name, not
the system ``Failsafe`` redefined, so no client of the system definition survives the merge."""

import xml.etree.ElementTree as ElementTree
from pathlib import Path
from typing import Final

from loguru import logger

from imbue.imbue_common.pure import pure
from screen.errors import XfconfChannelError
from screen.xfce_helpers import write_if_changed

XFCE_SESSION_CHANNEL_RELATIVE_PATH: Final[Path] = Path(".config/xfce4/xfconf/xfce-perchannel-xml/xfce4-session.xml")
NO_PANEL_SESSION_NAME: Final[str] = "ScreenNoPanel"
# The system failsafe session's members and priorities (xfce4-session's own xfce4-session.xml), less the panel and
# Thunar's daemon.
NO_PANEL_SESSION_CLIENTS: Final[tuple[tuple[str, int], ...]] = (("xfwm4", 15), ("xfsettingsd", 20), ("xfdesktop", 35))
_CHANNEL_NAME: Final[str] = "xfce4-session"
_CHANNEL_VERSION: Final[str] = "1.0"
_XML_DECLARATION: Final[str] = '<?xml version="1.1" encoding="UTF-8"?>\n\n'


def _child_property(parent: ElementTree.Element, name: str, property_type: str) -> ElementTree.Element:
    """The ``property`` child named ``name``, created with ``property_type`` when the parent lacks one."""
    for child in parent.findall("property"):
        if child.get("name") == name:
            return child
    return ElementTree.SubElement(parent, "property", {"name": name, "type": property_type})


def _set_value(parent: ElementTree.Element, name: str, property_type: str, value: str) -> None:
    child = _child_property(parent, name, property_type)
    child.set("type", property_type)
    child.set("value", value)


def _replace_session(sessions: ElementTree.Element) -> None:
    for child in sessions.findall("property"):
        if child.get("name") == NO_PANEL_SESSION_NAME:
            sessions.remove(child)
    session = ElementTree.SubElement(sessions, "property", {"name": NO_PANEL_SESSION_NAME, "type": "empty"})
    _set_value(session, "IsFailsafe", "bool", "true")
    _set_value(session, "Count", "int", str(len(NO_PANEL_SESSION_CLIENTS)))
    for index, (command, priority) in enumerate(NO_PANEL_SESSION_CLIENTS):
        command_property = ElementTree.SubElement(
            session, "property", {"name": f"Client{index}_Command", "type": "array"}
        )
        ElementTree.SubElement(command_property, "value", {"type": "string", "value": command})
        _set_value(session, f"Client{index}_Priority", "int", str(priority))
        _set_value(session, f"Client{index}_PerScreen", "bool", "false")


@pure
def render_xfce_session_channel(existing: str | None) -> str:
    """The channel file naming the panel-less session as the failsafe one, with saving off and every other property
    kept. Raises ``XfconfChannelError`` for a file that is not a channel's."""
    if existing is None or existing.strip() == "":
        root = ElementTree.Element("channel", {"name": _CHANNEL_NAME, "version": _CHANNEL_VERSION})
    else:
        try:
            root = ElementTree.fromstring(existing)
        except ElementTree.ParseError as e:
            raise XfconfChannelError(f"the channel file is not well-formed XML: {e}") from e
        if root.tag != "channel" or root.get("name") != _CHANNEL_NAME:
            raise XfconfChannelError(f"the file is not the {_CHANNEL_NAME} channel's")
    general = _child_property(root, "general", "empty")
    _set_value(general, "FailsafeSessionName", "string", NO_PANEL_SESSION_NAME)
    _set_value(general, "SaveOnExit", "bool", "false")
    _replace_session(_child_property(root, "sessions", "empty"))
    ElementTree.indent(root, space="  ")
    return _XML_DECLARATION + ElementTree.tostring(root, encoding="unicode") + "\n"


def write_xfce_session_channel(home: Path) -> None:
    """Render the channel file into ``home``; a file that cannot be read, parsed or written is logged and left
    alone, since the Screen works with the panel too."""
    path = home / XFCE_SESSION_CHANNEL_RELATIVE_PATH
    try:
        write_if_changed(path, render_xfce_session_channel)
    except (OSError, UnicodeDecodeError, XfconfChannelError) as e:
        logger.warning("Could not write the Screen's desktop session channel {}: {}", path, e)

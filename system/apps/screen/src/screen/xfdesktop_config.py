"""xfdesktop's icons are off (docs/system/specs/screen/spec.md section 4.3): a desktop icon wants a double click,
which a phone cannot give, and anything an icon would do belongs in the panel; a dropped file is shown in a file
manager window instead. The service sets the ``xfce4-desktop`` channel's icon style to none in the channel file
xfconfd reads when the session starts (``~/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml``), keeping
every other property the file holds, before each stack start."""

import xml.etree.ElementTree as ElementTree
from pathlib import Path
from typing import Final

from loguru import logger

from imbue.imbue_common.pure import pure
from screen.errors import XfconfChannelError
from screen.xfce_helpers import write_if_changed

XFDESKTOP_CHANNEL_RELATIVE_PATH: Final[Path] = Path(".config/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml")
_CHANNEL_NAME: Final[str] = "xfce4-desktop"
_CHANNEL_VERSION: Final[str] = "1.0"
_ICONS_PROPERTY: Final[str] = "desktop-icons"
_STYLE_PROPERTY: Final[str] = "style"
# xfdesktop's XfceDesktopIconStyle: 0 is no icons at all.
_ICON_STYLE_NONE: Final[str] = "0"
# What xfconfd itself writes at the top of a channel file.
_XML_DECLARATION: Final[str] = '<?xml version="1.1" encoding="UTF-8"?>\n\n'


def _child_property(parent: ElementTree.Element, name: str, property_type: str) -> ElementTree.Element:
    """The ``property`` child named ``name``, created with ``property_type`` when the parent lacks one."""
    for child in parent.findall("property"):
        if child.get("name") == name:
            return child
    return ElementTree.SubElement(parent, "property", {"name": name, "type": property_type})


@pure
def render_xfdesktop_channel(existing: str | None) -> str:
    """The channel file with ``/desktop-icons/style`` set to none and every other property kept. Raises
    ``XfconfChannelError`` for a file that is not a channel's."""
    if existing is None or existing.strip() == "":
        root = ElementTree.Element("channel", {"name": _CHANNEL_NAME, "version": _CHANNEL_VERSION})
    else:
        try:
            root = ElementTree.fromstring(existing)
        except ElementTree.ParseError as e:
            raise XfconfChannelError(f"the channel file is not well-formed XML: {e}") from e
        if root.tag != "channel" or root.get("name") != _CHANNEL_NAME:
            raise XfconfChannelError(f"the file is not the {_CHANNEL_NAME} channel's")
    icons = _child_property(root, _ICONS_PROPERTY, "empty")
    style = _child_property(icons, _STYLE_PROPERTY, "int")
    style.set("type", "int")
    style.set("value", _ICON_STYLE_NONE)
    ElementTree.indent(root, space="  ")
    return _XML_DECLARATION + ElementTree.tostring(root, encoding="unicode") + "\n"


def write_xfdesktop_channel(home: Path) -> None:
    """Render the channel file into ``home``; a file that cannot be read, parsed or written is logged and left
    alone, since the Screen itself does not depend on it."""
    path = home / XFDESKTOP_CHANNEL_RELATIVE_PATH
    try:
        write_if_changed(path, render_xfdesktop_channel)
    except (OSError, UnicodeDecodeError, XfconfChannelError) as e:
        logger.warning("Could not write the Screen's desktop channel {}: {}", path, e)

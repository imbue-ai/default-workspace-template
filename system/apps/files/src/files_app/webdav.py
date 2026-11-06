"""Reading a device's file server, which speaks WebDAV: a PROPFIND answers with one ``response`` per entry, the
first being the folder (or file) asked about."""

import urllib.parse
import xml.etree.ElementTree as ElementTree
from email.utils import parsedate_to_datetime
from typing import Final

from imbue.imbue_common.frozen_model import FrozenModel

_DAV: Final[str] = "{DAV:}"

PROPFIND_BODY: Final[bytes] = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/><d:getcontentlength/><d:getlastmodified/><d:getcontenttype/></d:prop>'
    b"</d:propfind>"
)


class DavEntry(FrozenModel):
    """One entry a PROPFIND described: its name, kind, size in bytes, and change time in ms."""

    name: str
    is_folder: bool
    size: int
    mtime: int
    content_type: str


def _text(element: ElementTree.Element | None) -> str:
    return "" if element is None or element.text is None else element.text.strip()


def _mtime_ms(raw: str) -> int:
    try:
        return int(parsedate_to_datetime(raw).timestamp() * 1000)
    except (TypeError, ValueError):
        return 0


def parse_propfind(body: bytes) -> list[DavEntry]:
    """The entries of a multistatus answer, the one asked about first. Each is named by the last segment of its
    href: the server roots its hrefs its own way (``/users/...`` for ``/Users/...``), so the rest is not ours."""
    root = ElementTree.fromstring(body)
    entries: list[DavEntry] = []
    for response in root.iter(f"{_DAV}response"):
        href = urllib.parse.urlsplit(_text(response.find(f"{_DAV}href"))).path
        name = urllib.parse.unquote(href.rstrip("/").rsplit("/", 1)[-1])
        prop = response.find(f"{_DAV}propstat/{_DAV}prop")
        is_folder = prop is not None and prop.find(f"{_DAV}resourcetype/{_DAV}collection") is not None
        length = _text(prop.find(f"{_DAV}getcontentlength") if prop is not None else None)
        modified = _text(prop.find(f"{_DAV}getlastmodified") if prop is not None else None)
        content_type = _text(prop.find(f"{_DAV}getcontenttype") if prop is not None else None)
        entries.append(
            DavEntry(
                name=name,
                is_folder=is_folder,
                size=int(length) if length.isdigit() else 0,
                mtime=_mtime_ms(modified),
                content_type=content_type,
            )
        )
    return entries

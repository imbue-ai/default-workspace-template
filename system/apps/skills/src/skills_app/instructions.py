"""A skill's instructions as its page shows them: the body of its SKILL.md, rendered from markdown.

A skill installed from elsewhere is content the workspace does not trust, so raw HTML in it is escaped rather than
rendered, and markdown-it's own link check drops ``javascript:`` and ``data:`` links. A link to a file beside the
skill (``references/setup.md``) or to an absolute path names a file on this machine rather than a page of this app,
so it becomes a ``file:`` URL, which the shell opens in Files.
"""

import posixpath
from collections.abc import Iterator
from collections.abc import Sequence
from pathlib import Path
from typing import Final
from urllib.parse import quote
from urllib.parse import unquote
from urllib.parse import urlsplit

from markdown_it import MarkdownIt
from markdown_it.token import Token

from imbue.mngr.utils.agent_skills import split_skill_frontmatter

_MARKDOWN: Final[MarkdownIt] = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])


def render_instructions(content: str, skill_directory: Path) -> str:
    """The HTML for a SKILL.md's body, its frontmatter left out, with its file links pointing into ``skill_directory``."""
    tokens = _MARKDOWN.parse(split_skill_frontmatter(content).body)
    for token in _all_tokens(tokens):
        href = token.attrGet("href") if token.type == "link_open" else None
        file_url = _file_url_of(str(href), skill_directory) if href else None
        if file_url is not None:
            token.attrSet("href", file_url)
    return _MARKDOWN.renderer.render(tokens, _MARKDOWN.options, {})


def _all_tokens(tokens: Sequence[Token]) -> Iterator[Token]:
    for token in tokens:
        yield token
        if token.children:
            yield from _all_tokens(token.children)


def _file_url_of(href: str, skill_directory: Path) -> str | None:
    """The ``file:`` URL a link names when it is a path, or None for a link to anywhere else (a site, an anchor)."""
    parts = urlsplit(href)
    if parts.scheme or parts.netloc or not parts.path:
        return None
    path = unquote(parts.path)
    if not path.startswith("/"):
        path = posixpath.normpath(posixpath.join(skill_directory.as_posix(), path))
    return f"file://{quote(path)}"

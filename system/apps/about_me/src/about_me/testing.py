"""Sample files for the tests: a profile with every line shape, a memory index, and sources with odd entries."""

from pathlib import Path

SAMPLE_PROFILE = (
    b"---\n"
    b"name: user-public-profile\n"
    b"description: What the public web says about the user, from a lookup they asked for\n"
    b"metadata:\n"
    b"  type: user\n"
    b"- not a fact: inside the frontmatter\n"
    b"---\n"
    b"\n"
    b"- Designs typefaces in Berlin ([Portfolio](https://example.com/portfolio)) <!-- found 2026-11-01 -->\n"
    b"- Writes a newsletter about type ([Newsletter](https://example.com/news)) <!-- found 2026-11-02 -->\n"
    b"- Teaches a weekend lettering class ([Portfolio](https://example.com/portfolio)) <!-- found 2026-11-01 -->\n"
    b"- Is learning Japanese (you told me) <!-- found 2026-11-03 -->\n"
    b"- Something written by hand, with no link\n"
    b"\n"
    b"These facts came from a web lookup you asked for on 2026-11-03.\n"
)
SAMPLE_INDEX = (
    b"- [Coding style](coding-style.md) -- how the user likes code\n"
    b"- [Public profile](user-public-profile.md) -- what the web lookup found\n"
    b"- [Projects](projects.md) -- what the user is building\n"
)
SAMPLE_SOURCES = (
    b'[\n  {"url": "https://example.com/portfolio", "title": "Portfolio", "fetched_at": "2026-11-01", '
    b'"origin": "page"},\n'
    b'  {"url": "https://example.com/news", "title": "Newsletter", "fetched_at": "2026-11-02", "origin": "search", '
    b'"snippet": "an extra key"},\n'
    b'  {"url": null, "title": "What you told me", "fetched_at": "2026-11-03", "origin": "user"},\n'
    b'  {"title": "No url key at all"}\n]\n'
)


def write_workspace_file(root: Path, relative: Path, content: bytes) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path

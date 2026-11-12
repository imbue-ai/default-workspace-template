"""Every right-click menu in the workspace is the element menu's (.agents/shared/references/right-click-menus.md).

A page that calls ``preventDefault`` on a ``contextmenu`` event draws a menu of its own in the element menu's place.
That menu has none of the element menu's standard and reference rows, hands no right-click on text or a link to the
Imbue Studio desktop app's native menu, and is free to use rows the element menu does not take, which the menu of an
app without the shared Menu component then cannot show. The element menu (``installElementContextMenu``) takes an
app's own rows instead, so every app's menu is built from the same rows. So every ``contextmenu`` handler that
prevents the default, in the template's apps and the user's alike, fails here unless a marker among the comment
lines directly above it names why it has to, ``// own-context-menu: <reason> -- <why>``.

The scan reads source text, a page kept as a string in a Python file (a scaffolded app's ``runner.py``) included:
an inline handler, or one named by an identifier defined in the same file, is read for a ``preventDefault`` call,
and one bound as a property or an attribute also for a returned ``false``, which prevents the default there; a
handler reached any other way (a property of an object, an import) is not followed.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import NamedTuple

from entry_points_testing import REPO_ROOT

_MARKER = "own-context-menu"
_REASONS = frozenset(
    {"forwards", "dismisses", "touch", "custom-content", "element-menu"}
)
# Any comment that opens like a marker is one, so a malformed marker is reported rather than passed over.
_MARKER_RE = re.compile(r"^\s*(?://|/?\*|<!--)\s*own-context-menu:(.*?)(?:-->)?\s*$")
_COMMENT_RE = re.compile(r"^\s*(?://|/\*|\*|<!--)")

_SOURCE_SUFFIXES = (
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".html",
    ".vue",
    ".svelte",
    ".py",
)
_SKIPPED_PARTS = frozenset({"vendor", "node_modules", "dist"})

# Where a handler starts: a DOM or Mithril ``oncontextmenu`` (property, attribute, or attrs key), a JSX
# ``onContextMenu``, or a ``contextmenu`` listener.
_HANDLER_START_RE = re.compile(
    r"""\b(?:oncontextmenu|onContextMenu)\s*(?::|=(?!=))\s*"""
    r"""|\baddEventListener\(\s*["'`]contextmenu["'`]\s*,\s*"""
)
_DECLARATION_BEFORE_RE = re.compile(r"\b(?:const|let|var|function)\s+$")
_RETURNS_FALSE_RE = re.compile(r"\breturn\s+false\b|=>\s*false\b")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_$][\w$]*$")
_OPENERS = {"(": ")", "[": "]", "{": "}"}
_CLOSERS = frozenset(_OPENERS.values())


class _Marker(NamedTuple):
    line: int
    reason: str
    why: str

    def is_valid(self) -> bool:
        return self.reason in _REASONS and bool(self.why)


class _Site(NamedTuple):
    relative: str
    line: int
    text: str
    markers: tuple[_Marker, ...]

    def is_kept(self) -> bool:
        return any(marker.is_valid() for marker in self.markers)

    def describe(self) -> str:
        return f"{self.relative}:{self.line}: {self.text}"


def _skip_string(source: str, index: int) -> int:
    """The index just past the string literal opening at ``index``."""
    quote = source[index]
    index += 1
    while index < len(source) and source[index] != quote:
        index += 2 if source[index] == "\\" else 1
    return index + 1


def _expression_end(source: str, start: int) -> int:
    """Where the expression starting at ``start`` ends: at the first ``,``, ``;``, or unmatched closer outside any
    bracket, string, or comment, or at the end of a line that leaves no bracket open."""
    depth = 0
    index = start
    while index < len(source):
        char = source[index]
        if char in "\"'`":
            index = _skip_string(source, index)
            continue
        if source.startswith("//", index):
            newline = source.find("\n", index)
            index = len(source) if newline == -1 else newline
            continue
        if source.startswith("/*", index):
            close = source.find("*/", index + 2)
            index = len(source) if close == -1 else close + 2
            continue
        if char in _OPENERS:
            depth += 1
        elif char in _CLOSERS:
            if depth == 0:
                return index
            depth -= 1
        elif depth == 0 and char in ",;":
            return index
        elif (
            depth == 0
            and char == "\n"
            and source[start:index].strip()
            and not _continues_past(source, start, index)
        ):
            return index
        index += 1
    return index


# What ends a line, or opens the next, of an expression that carries on across the newline: an arrow whose body
# prettier moved down, or a condition wrapped over several lines.
_CONTINUING_LINE_ENDS = ("=>", "&&", "||", "??", "?", ":", "=", "+")
_CONTINUING_LINE_STARTS = ("?", ":", ".", "&&", "||", "??", "+")


def _continues_past(source: str, start: int, newline: int) -> bool:
    """Whether the expression from ``start`` carries on across the newline at ``newline``."""
    if source[start:newline].rstrip().endswith(_CONTINUING_LINE_ENDS):
        return True
    return source[newline:].lstrip().startswith(_CONTINUING_LINE_STARTS)


def _definition_of(source: str, name: str) -> str | None:
    """The text of ``name``'s function in ``source``, defined by a declaration or a ``const``/``let``/``var``."""
    match = re.search(
        rf"\bfunction\s+{re.escape(name)}\b|\b(?:const|let|var)\s+{re.escape(name)}\b[^=;]*=",
        source,
    )
    if match is None:
        return None
    return source[match.end() : _expression_end(source, match.end())]


def _handler_text(source: str, start: int, is_html: bool) -> str:
    """The handler a site names: its inline expression, a quoted attribute in HTML, or a named function's text, the
    name bare or in JSX braces."""
    if is_html and source[start : start + 1] in "\"'":
        return source[start + 1 : _skip_string(source, start) - 1]
    if source[start : start + 1] == "{":
        braced = source[start + 1 : _expression_end(source, start + 1)].strip()
        if _IDENTIFIER_RE.match(braced):
            return _definition_of(source, braced) or ""
    expression = source[start : _expression_end(source, start)].strip()
    if _IDENTIFIER_RE.match(expression):
        return _definition_of(source, expression) or ""
    return expression


def _markers_above(lines: list[str], line: int) -> tuple[_Marker, ...]:
    """The markers among the unbroken run of comment lines directly above ``line``."""
    markers: list[_Marker] = []
    number = line - 1
    while number >= 1 and _COMMENT_RE.match(lines[number - 1]):
        match = _MARKER_RE.match(lines[number - 1])
        if match:
            reason, _, why = match.group(1).partition("--")
            markers.append(_Marker(number, reason.strip(), why.strip()))
        number -= 1
    return tuple(markers)


def _sites_in(relative: str, source: str) -> list[_Site]:
    """The ``contextmenu`` handlers in ``source`` that prevent the default, marked or not."""
    lines = source.splitlines()
    is_html = relative.endswith((".html", ".vue", ".svelte"))
    sites: list[_Site] = []
    for match in _HANDLER_START_RE.finditer(source):
        # A variable of that name (``const onContextMenu = ...``) is a handler only where something listens with it.
        if _DECLARATION_BEFORE_RE.search(source, 0, match.start()):
            continue
        handler = _handler_text(source, match.end(), is_html)
        # False returned from a handler bound as a property or an attribute prevents the default too (Mithril does
        # the same for its attrs); a listener's return value and a JSX handler's are ignored.
        is_bound_as_property = match.group().startswith("oncontextmenu") or (
            is_html and not match.group().startswith("addEventListener")
        )
        if "preventDefault" not in handler and not (
            is_bound_as_property and _RETURNS_FALSE_RE.search(handler)
        ):
            continue
        line = source.count("\n", 0, match.start()) + 1
        sites.append(
            _Site(relative, line, lines[line - 1].strip(), _markers_above(lines, line))
        )
    return sites


def _is_scanned(relative: str) -> bool:
    path = PurePosixPath(relative)
    if _SKIPPED_PARTS.intersection(path.parts) or path.name.endswith(".d.ts"):
        return False
    return not (
        ".test." in path.name
        or ".spec." in path.name
        or path.name.startswith("test_")
        or path.name.endswith("_test.py")
    )


def _tracked_sources(repo_root: Path) -> list[str]:
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "--", *(f"*{suffix}" for suffix in _SOURCE_SUFFIXES)],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    return sorted(
        relative
        for relative in tracked
        if relative and _is_scanned(relative) and (repo_root / relative).is_file()
    )


def _sites(repo_root: Path) -> list[_Site]:
    return [
        site
        for relative in _tracked_sources(repo_root)
        for site in _sites_in(
            relative, (repo_root / relative).read_text(errors="replace")
        )
    ]


def _marker_problems(repo_root: Path) -> list[str]:
    """Markers that keep nothing: an unknown reason, no why, or no handler directly below."""
    problems: list[str] = []
    for relative in _tracked_sources(repo_root):
        source = (repo_root / relative).read_text(errors="replace")
        if _MARKER not in source:
            continue
        attached = {
            marker.line
            for site in _sites_in(relative, source)
            for marker in site.markers
        }
        for number, line in enumerate(source.splitlines(), start=1):
            match = _MARKER_RE.match(line)
            if match is None:
                continue
            reason, _, why = match.group(1).partition("--")
            where = f"{relative}:{number}"
            if not _Marker(number, reason.strip(), why.strip()).is_valid():
                problems.append(
                    f"{where}: needs `<reason> -- <why>`, <reason> one of {', '.join(sorted(_REASONS))}"
                )
            elif number not in attached:
                problems.append(
                    f"{where}: keeps nothing; it belongs in the comment lines directly above the handler it keeps"
                )
    return problems


_GUIDE = """\
The goal is one right-click menu across the workspace, the element menu: a page's own rows for what was clicked,
then the standard rows (Copy, Paste, the link and image rows), then "Copy reference" / "Explain..." / "Modify...",
with a right-click on text or a plain link left to the Imbue Studio desktop app's native menu where it draws one.
A page that prevents the right-click's default draws a menu without any of that. Move each menu's rows into the
element menu instead: drop the handler's preventDefault (and its menu) and return the rows from
`installElementContextMenu`'s `extraRows(target)`, reading what was clicked off `target.element` (a `data-*`
attribute on the row). Rows can be actions with an icon and a tone, check rows, and submenus of those
(.agents/shared/references/right-click-menus.md). You may restructure the page's menu code to get there, even when
it is not the code you came to change. A handler that has to keep the right-click takes
`// own-context-menu: <reason> -- <why>` in the comment lines directly above it, with <reason> one of: {reasons}.
Name every marker you add in your report. Never edit this test to let a site through.
"""


def test_no_page_draws_its_own_right_click_menu_without_a_reason() -> None:
    sites = [site.describe() for site in _sites(REPO_ROOT) if not site.is_kept()]
    assert not sites, (
        "These right-click handlers prevent the default, and so draw a menu of their own in the element menu's place:\n"
        + "\n".join(sites)
        + "\n\n"
        + _GUIDE.format(reasons=", ".join(sorted(_REASONS)))
    )


def test_every_marker_keeps_a_handler_for_a_named_reason() -> None:
    assert _marker_problems(REPO_ROOT) == []


def test_the_scan_sees_each_form_of_handler_and_what_keeps_one(tmp_path: Path) -> None:
    files = {
        "app/mithril.ts": (
            "m('div', {\n"
            "  oncontextmenu: (event: MouseEvent) => {\n"
            "    event.preventDefault();\n"
            "    openMenu(event);\n"
            "  },\n"
            "  onclick: () => event.preventDefault(),\n"
            "});\n"
            "m('li', { oncontextmenu: () => select(row) });\n"
            "m('li', { oncontextmenu: attrs.onContextMenu });\n"
            "function onPress(event: MouseEvent): void {\n"
            "  event.preventDefault();\n"
            "}\n"
            "row.addEventListener('contextmenu', onPress);\n"
            "m('li', { oncontextmenu: () => false });\n"
            "m('li', {\n"
            "  oncontextmenu: () => {\n"
            "    select(row);\n"
            "    return false;\n"
            "  },\n"
            "});\n"
        ),
        "app/listeners.js": (
            "stage.addEventListener('contextmenu', (event) => event.preventDefault());\n"
            "const onRightClick = (event) => {\n"
            "  if (busy) return;\n"
            "  event.preventDefault();\n"
            "};\n"
            'document.addEventListener("contextmenu", onRightClick);\n'
            "function quiet(event) {\n"
            "  console.log(event);\n"
            "}\n"
            "document.addEventListener('contextmenu', quiet);\n"
            "const onContextMenu = (event) => event.preventDefault();\n"
            "element.oncontextmenu = function (event) { event.preventDefault(); };\n"
            "card.addEventListener('contextmenu', onContextMenu);\n"
            "stage.addEventListener('contextmenu', () => false);\n"
        ),
        "app/page.html": (
            '<div oncontextmenu="event.preventDefault(); openMenu()">x</div>\n'
            '<div oncontextmenu="highlight()">y</div>\n'
            '<canvas oncontextmenu="return false"></canvas>\n'
        ),
        "app/view.tsx": (
            "<li onContextMenu={(e) => { e.preventDefault(); show(e); }}>z</li>\n"
            "const onRow = (e) => { e.preventDefault(); };\n"
            "<li onContextMenu={onRow}>w</li>\n"
            "<li onContextMenu={() => false}>v</li>\n"
        ),
        "app/wrapped.ts": (
            'stage.addEventListener("contextmenu", (event) =>\n'
            "  helper(event, () => event.preventDefault()),\n"
            ");\n"
            "m('li', {\n"
            "  oncontextmenu:\n"
            "    isOn &&\n"
            "    hasShortcut\n"
            "      ? (event) => {\n"
            "          event.preventDefault();\n"
            "        }\n"
            "      : undefined,\n"
            "});\n"
            "element.oncontextmenu = select\n"
            "cancel.onclick = (event) => event.preventDefault()\n"
        ),
        "app/runner.py": (
            'PAGE = f"""<!doctype html><body>\n'
            '<div oncontextmenu="event.preventDefault(); openMenu()">x</div>\n'
            '<script type="module">\n'
            "  // own-context-menu: forwards -- the canvas takes the right-click as a stroke\n"
            '  canvas.addEventListener("contextmenu", (event) => event.preventDefault());\n'
            '  list.addEventListener("contextmenu", (event) => {{\n'
            "    event.preventDefault();\n"
            "  }});\n"
            "</script></body>\n"
            '"""\n'
        ),
        "app/marked.ts": (
            "// The stage hands the right-click to the remote desktop.\n"
            "// own-context-menu: forwards -- the remote desktop takes the right-click\n"
            "stage.addEventListener('contextmenu', (event) => event.preventDefault());\n"
            "// own-context-menu: faster -- not a reason\n"
            "card.addEventListener('contextmenu', (event) => event.preventDefault());\n"
            "// own-context-menu: dismisses\n"
            "sheet.addEventListener('contextmenu', (event) => event.preventDefault());\n"
            "// own-context-menu: touch -- separated from its handler\n"
            "\n"
            "long.addEventListener('contextmenu', (event) => event.preventDefault());\n"
            "// own-context-menu: custom-content -- above nothing that prevents\n"
            "row.addEventListener('contextmenu', () => select());\n"
        ),
    }
    for relative, text in files.items():
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_text(text)
    found = {
        (site.relative, site.line, site.is_kept())
        for relative, text in files.items()
        for site in _sites_in(relative, text)
    }
    assert found == {
        ("app/mithril.ts", 2, False),
        ("app/mithril.ts", 13, False),
        ("app/mithril.ts", 14, False),
        ("app/mithril.ts", 16, False),
        ("app/listeners.js", 1, False),
        ("app/listeners.js", 6, False),
        ("app/listeners.js", 12, False),
        ("app/listeners.js", 13, False),
        ("app/page.html", 1, False),
        ("app/page.html", 3, False),
        ("app/view.tsx", 1, False),
        ("app/view.tsx", 3, False),
        ("app/wrapped.ts", 1, False),
        ("app/wrapped.ts", 5, False),
        ("app/runner.py", 2, False),
        ("app/runner.py", 5, True),
        ("app/runner.py", 6, False),
        ("app/marked.ts", 3, True),
        ("app/marked.ts", 5, False),
        ("app/marked.ts", 7, False),
        ("app/marked.ts", 10, False),
    }
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    assert _marker_problems(tmp_path) == [
        "app/marked.ts:4: needs `<reason> -- <why>`, <reason> one of "
        "custom-content, dismisses, element-menu, forwards, touch",
        "app/marked.ts:6: needs `<reason> -- <why>`, <reason> one of "
        "custom-content, dismisses, element-menu, forwards, touch",
        "app/marked.ts:8: keeps nothing; it belongs in the comment lines directly above the handler it keeps",
        "app/marked.ts:11: keeps nothing; it belongs in the comment lines directly above the handler it keeps",
    ]


def test_tests_vendored_code_and_builds_are_not_scanned() -> None:
    assert _is_scanned("system/apps/notes/frontend/src/menu.ts")
    assert _is_scanned("system/apps/notes/templates/index.html")
    assert _is_scanned("system/apps/notes/src/notes/runner.py")
    for relative in (
        "system/apps/notes/frontend/src/menu.test.ts",
        "system/apps/notes/frontend/e2e/menu.spec.ts",
        "system/apps/notes/src/notes/runner_test.py",
        "system/apps/notes/test_notes.py",
        "system/vendor/neko/client.js",
        "system/apps/notes/frontend/node_modules/lib/index.js",
        "system/apps/notes/frontend/dist/app.js",
        "system/libs/workspace_ui/src/embed-contract.d.ts",
    ):
        assert not _is_scanned(relative), relative

"""Project-specific ratchets holding the shell's mngr-free invariant (the workspace app model, plan section 3.5).

The shell imports nothing from mngr and nothing from the chat app, and never runs the ``mngr``
binary: everything that concerns chats and agents lives inside the chat app, so the chat app
can be replaced without touching the shell. The import half walks every non-test module's
import statements (an AST scan rather than an import-linter contract: grimp's scanner panics
on this package once external packages are included); the subprocess half is a regex over
the same sources. Lives outside ``test_ratchets.py`` because that file must define the same
test set across every project.
"""

import ast
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest
from inline_snapshot import snapshot

from imbue.imbue_common.ratchet_testing.common_ratchets import RatchetRuleInfo
from imbue.imbue_common.ratchet_testing.core import FileExtension
from imbue.imbue_common.ratchet_testing.core import RegexPattern
from imbue.imbue_common.ratchet_testing.core import check_regex_ratchet

_PACKAGE = Path(__file__).parent

pytestmark = pytest.mark.xdist_group(name="ratchets")

# What the shell must never import: mngr and its plugins (agents are the chat app's business;
# every one of them is a module whose name starts with ``imbue.mngr``: ``imbue.mngr``,
# ``imbue.mngr_claude``, ...) and the chat app itself (an app the shell knows only through the
# registry and its APIs).
_FORBIDDEN_MODULE_FAMILY_PREFIX: Final[str] = "imbue.mngr"
_FORBIDDEN_PACKAGES: Final[tuple[str, ...]] = ("imbue.chat",)

# The directory the top-level ``imbue`` package lives in: what a module's absolute name is
# spelled relative to.
_PACKAGE_ROOT = _PACKAGE.parent.parent

_TEST_FILE_PATTERNS: Final[tuple[str, ...]] = ("*_test.py", "test_*.py", "testing.py", "conftest.py")

_MNGR_SUBPROCESS_RULE = RatchetRuleInfo(
    rule_name="the shell running the mngr binary",
    rule_description=(
        "The shell never runs ``mngr``: agents are the chat app's instances, and every verb on them "
        "(create, rename, destroy, stop, start, message) goes through the chat app's instances API or "
        "its own routes. A subprocess call whose argv starts with 'mngr' belongs in the chat package."
    ),
)

# An argv literal that starts with the mngr binary: ``["mngr", ...`` or ``("mngr", ...``. A prose
# mention (the not-built placeholder's repair suggestion) is a string, not an argv, so it does
# not match.
_MNGR_ARGV_PATTERN = RegexPattern(r"""[\[(]\s*["']mngr["']\s*,""", multiline=False)


def _is_test_file(path: Path) -> bool:
    return any(path.match(pattern) for pattern in _TEST_FILE_PATTERNS)


def _import_from_base(source_file: Path, package_root: Path, node: ast.ImportFrom) -> str:
    """The absolute module a ``from ... import`` names: a relative import is resolved from the
    importing file's own package (``package_root`` holds the top-level ``imbue`` package)."""
    if node.level == 0:
        return node.module or ""
    package_parts = list(source_file.relative_to(package_root).with_suffix("").parts[:-1])
    base_parts = package_parts[: len(package_parts) - (node.level - 1)]
    if node.module:
        base_parts.append(node.module)
    return ".".join(base_parts)


def _imported_module_names(source_file: Path, package_root: Path = _PACKAGE_ROOT) -> Iterator[str]:
    """Every absolute name a module's import statements reach: the module of an ``import``, and
    for a ``from`` import both its base and ``base.name`` per imported name, so ``imbue.chat`` is
    seen behind ``from imbue import chat``."""
    for node in ast.walk(ast.parse(source_file.read_text())):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            base = _import_from_base(source_file, package_root, node)
            if base:
                yield base
            for alias in node.names:
                yield f"{base}.{alias.name}" if base else alias.name


def _is_forbidden(module_name: str) -> bool:
    if module_name.startswith(_FORBIDDEN_MODULE_FAMILY_PREFIX):
        return True
    return any(module_name == package or module_name.startswith(f"{package}.") for package in _FORBIDDEN_PACKAGES)


def _forbidden_imports(source_file: Path, package_root: Path = _PACKAGE_ROOT) -> set[str]:
    return {name for name in _imported_module_names(source_file, package_root) if _is_forbidden(name)}


def test_the_shell_imports_neither_mngr_nor_the_chat_app() -> None:
    """Every non-test module of the shell imports nothing under ``imbue.mngr*`` or ``imbue.chat``."""
    offenders = sorted(
        f"{source_file.relative_to(_PACKAGE)}: {module_name}"
        for source_file in _PACKAGE.rglob("*.py")
        if not _is_test_file(source_file)
        for module_name in _forbidden_imports(source_file)
    )
    assert offenders == [], "the shell imports what it must not:\n" + "\n".join(f"  - {line}" for line in offenders)


def test_the_import_scan_sees_every_spelling_of_a_forbidden_import(tmp_path: Path) -> None:
    """The plugins (``imbue.mngr_*``), ``from imbue import ...``, and relative imports are all caught;
    the shared library and a sibling module are not."""
    module = tmp_path / "imbue" / "system_interface" / "shell" / "offender.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from imbue.mngr_codex.app_server_client import CodexModel\n"
        "from imbue import chat\n"
        "from ...chat import models\n"
        "import imbue.mngr.primitives\n"
        "from imbue.imbue_common.pure import pure\n"
        "from . import layout_ops\n"
        "from ..config import Config\n"
    )

    assert _forbidden_imports(module, tmp_path) == {
        "imbue.chat",
        "imbue.chat.models",
        "imbue.mngr.primitives",
        "imbue.mngr_codex.app_server_client",
        "imbue.mngr_codex.app_server_client.CodexModel",
    }


def test_prevent_mngr_subprocess_invocations() -> None:
    chunks = check_regex_ratchet(_PACKAGE, FileExtension(".py"), _MNGR_ARGV_PATTERN, _TEST_FILE_PATTERNS)
    assert len(chunks) <= snapshot(0), _MNGR_SUBPROCESS_RULE.format_failure(chunks)


_FRONTEND_SRC = _PACKAGE.parent.parent / "frontend" / "src"

_SHELL_NAMES_THE_CHAT_RULE = RatchetRuleInfo(
    rule_name="the shell naming the chat app",
    rule_description=(
        "The shell knows no app by name: the chat is an app like the terminal or the files app, found "
        "through the registry and known only as the app name a window or a requester carries. A literal "
        "'chat' in the shell package or its frontend is the shell special-casing one app; carry the name "
        "the request came with instead (a layout op's requester, a window's app)."
    ),
)

# The bare app name as a string literal, and the name as the app of a retired address literal
# (``"app:chat"``, ``"app:chat?instance=..."``, a template literal's ``app:chat?``). Class names
# such as "chat-panel" and prose (``chat`` in a docstring) do not match: the bare form takes a
# string quote on both sides, and the address form is closed by a quote or its ``?`` at once.
_CHAT_NAME_LITERAL = re.compile(r"""["']chat["']|app:chat(?:["'`]|\?)""")


def _frontend_source_files() -> Iterator[Path]:
    """The shell frontend's own sources: not its tests."""
    for source_file in _FRONTEND_SRC.rglob("*.ts"):
        if not source_file.name.endswith(".test.ts"):
            yield source_file


@pytest.mark.parametrize(
    ("line", "is_named"),
    [
        ('const app = "chat";', True),
        ("const app = 'chat';", True),
        ('const address = "app:chat?instance=" + key;', True),
        ("const address = `app:chat?instance=${key}`;", True),
        ('open("app:chat");', True),
        ('const panel = "chat-panel";', False),
        ("# the chat app's own page", False),
        ("# the ``chat`` template", False),
        ('const address = "app:terminal?instance=" + key;', False),
    ],
)
def test_the_chat_name_pattern_catches_the_name_in_an_address_and_not_in_prose(line: str, is_named: bool) -> None:
    assert (_CHAT_NAME_LITERAL.search(line) is not None) is is_named


_PIXEL_METRIC_RULE = RatchetRuleInfo(
    rule_name="literal pixel lengths in the desktop's views and reducers",
    rule_description=(
        "Every metric the desktop's behaviour needs (title bar and taskbar heights, cell sizes, the grid "
        "inset, minimum window size, snap and drag thresholds, the touch target) is a token in "
        "frontend/src/theme/default.css, read once into ThemeMetrics by theme/metrics.ts and handed to the "
        "views and reducers by the store (desktop-interface plan section 6.6). A `px` length written in a "
        "string in views/ or reducers/ (a Tailwind utility such as `h-[36px]`, an inline style, a class) is a "
        "metric living in two places: reference the token (a `--desk-*` utility or the store's metrics) "
        "instead. Only lengths spelled with `px` are checked; a bare number the code treats as pixels is not."
    ),
)

# A pixel length inside a balanced string literal on a code line: a Tailwind utility (``h-[36px]``),
# an inline style, a class. Comment lines and trailing comments do not count, and neither do the
# container-query breakpoints (``@max-[620px]``), which are breakpoints rather than metrics and,
# like the compact breakpoint, live in the code by design. The length is bounded by lookarounds
# rather than ``\b`` so an underscore-joined arbitrary value (``shadow-[0_2px_8px_...]``) counts.
_PIXEL_METRIC_PATTERN = RegexPattern(
    r"""^(?![ \t]*(?://|\*|/\*)).*?(["'`])(?:(?!\1)[^\n])*?(?<!@max-\[)(?<!@min-\[)(?<![A-Za-z0-9.])\d+(?:\.\d+)?px(?![A-Za-z0-9])(?:(?!\1)[^\n])*\1""",
    multiline=True,
)

_DESKTOP_METRIC_FREE_DIRECTORIES: Final[tuple[str, ...]] = ("views", "reducers")


def test_prevent_pixel_metrics_in_views_and_reducers() -> None:
    chunks = [
        chunk
        for directory in _DESKTOP_METRIC_FREE_DIRECTORIES
        for chunk in check_regex_ratchet(
            _FRONTEND_SRC / directory, FileExtension(".ts"), _PIXEL_METRIC_PATTERN, ("*.test.ts",)
        )
    ]
    assert len(chunks) <= snapshot(0), _PIXEL_METRIC_RULE.format_failure(tuple(chunks))


@pytest.mark.parametrize(
    ("line", "is_metric"),
    [
        ('class: "h-[36px] w-full",', True),
        ("style: `top: 0px`,", True),
        ('const TITLE_BAR = "36px";', True),
        ('class: "@max-[620px]:w-1/2 h-9",', False),
        ("// the bar is 36px tall", False),
        (" * 24px would not be visible", False),
        ("style: { left: `${rect.x}px` },", False),
        ('class: "h-(--desk-title-bar-height)",', False),
        ('const x = "a"; // 36px tall', False),
        ('m("div", { class: "h-9" }), // pad 12px', False),
        ('class: "it\'s 36px wide",', True),
        ('class: "shadow-[0_2px_8px_rgb(0,0,0,0.2)]",', True),
        ('class: "[text-shadow:var(--desk-shortcut-label-shadow)]",', False),
    ],
)
def test_the_pixel_metric_pattern_catches_a_literal_and_not_a_breakpoint_or_a_comment(
    line: str, is_metric: bool
) -> None:
    assert (_PIXEL_METRIC_PATTERN.compiled.search(line) is not None) is is_metric


def test_the_shell_names_no_app() -> None:
    offenders = sorted(
        f"{source_file.relative_to(_PACKAGE.parent.parent)}:{line_number}"
        for source_file in (
            *(path for path in _PACKAGE.rglob("*.py") if not _is_test_file(path)),
            *_frontend_source_files(),
        )
        for line_number, line in enumerate(source_file.read_text().splitlines(), start=1)
        if _CHAT_NAME_LITERAL.search(line)
    )
    assert offenders == [], (
        _SHELL_NAMES_THE_CHAT_RULE.rule_description + "\n" + "\n".join(f"  - {line}" for line in offenders)
    )

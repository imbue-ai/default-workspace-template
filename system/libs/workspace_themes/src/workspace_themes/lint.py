import re
from pathlib import Path
from typing import Final

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from pydantic import Field

# The files a tokens-mode app's look lives in (docs/system/blueprint/workspace-themes/, section 7): its frontend
# sources, and its Python modules, where a page built by the build-app scaffold keeps its markup inline.
LINTED_SUFFIXES: Final[frozenset[str]] = frozenset(
    {".ts", ".tsx", ".js", ".css", ".html", ".py"}
)
# Built output, dependencies, and tests carry literals that never reach a themed page.
SKIPPED_DIRECTORY_NAMES: Final[frozenset[str]] = frozenset(
    {"node_modules", "static", "dist", "build", "assets"}
)
SKIPPED_FILE_SUFFIXES: Final[tuple[str, ...]] = (
    ".test.ts",
    ".test.tsx",
    ".spec.ts",
    ".d.ts",
    "_test.py",
)
SKIPPED_FILE_PREFIXES: Final[tuple[str, ...]] = ("test_",)

# Values that name no look of their own: a property set to one of these is not a literal.
_CSS_WIDE_KEYWORDS: Final[str] = r"inherit|initial|unset|revert"

_LITERAL_PATTERNS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    (
        re.compile(
            r"(?<![\w&/-])#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{4}|[0-9a-fA-F]{3})\b"
        ),
        "a literal color; use a color token (text-primary, bg-surface, var(--c-...))",
    ),
    (
        re.compile(r"\b(?:rgba?|hsla?|oklch|oklab)\("),
        "a literal color; use a color token",
    ),
    (
        re.compile(rf"\bfont-family\s*:(?!\s*(?:var\(|(?:{_CSS_WIDE_KEYWORDS})\b))"),
        "a font family; use the type tokens (font-sans, font-mono)",
    ),
    (
        re.compile(
            rf"\bbox-shadow\s*:(?!\s*(?:var\(|(?:none|{_CSS_WIDE_KEYWORDS})\b))"
        ),
        "a literal shadow; use shadow-raised or shadow-overlay",
    ),
    (
        re.compile(r"\b(?:bg|text|border|fill|stroke|from|to|via|ring|outline)-\[#"),
        "a literal color in a utility",
    ),
    (
        re.compile(r"\bfont-\['"),
        "a font family in a utility; use font-sans or font-mono",
    ),
)
# A line comment in TypeScript or CSS, or in Python (`# ` with its space: a CSS id selector has none).
_COMMENT_LINE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\s*(?://|/?\*|#\s)")


class ThemingLintFinding(FrozenModel):
    """A literal look in an app's frontend that a theme cannot reach."""

    path: Path = Field(description="The file")
    line_number: int = Field(description="The line, from 1")
    message: str = Field(description="What the literal is and what to use instead")
    line: str = Field(description="The line's text, trimmed")


@pure
def find_literal_looks_in_text(path: Path, text: str) -> list[ThemingLintFinding]:
    findings: list[ThemingLintFinding] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if _COMMENT_LINE_PATTERN.match(line):
            continue
        for pattern, message in _LITERAL_PATTERNS:
            if pattern.search(line):
                findings.append(
                    ThemingLintFinding(
                        path=path,
                        line_number=line_number,
                        message=message,
                        line=line.strip(),
                    )
                )
                break
    return findings


def _is_linted_file(path: Path, app_directory: Path) -> bool:
    if (
        path.suffix not in LINTED_SUFFIXES
        or path.name.endswith(SKIPPED_FILE_SUFFIXES)
        or path.name.startswith(SKIPPED_FILE_PREFIXES)
    ):
        return False
    relative_parts = path.relative_to(app_directory).parts
    return not any(part in SKIPPED_DIRECTORY_NAMES for part in relative_parts[:-1])


def lint_app_frontend(app_directory: Path) -> list[ThemingLintFinding]:
    """Every literal color, font family, and shadow in an app's frontend sources."""
    findings: list[ThemingLintFinding] = []
    for path in sorted(app_directory.rglob("*")):
        if not path.is_file() or not _is_linted_file(path, app_directory):
            continue
        findings.extend(
            find_literal_looks_in_text(
                path, path.read_text(encoding="utf-8", errors="replace")
            )
        )
    return findings

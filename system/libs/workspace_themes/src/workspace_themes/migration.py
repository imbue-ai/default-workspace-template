"""Contract 1 to contract 2 (docs/system/blueprint/brand-design-system/): rewrites a theme folder's token names in
place, keeping everything else in its files as written."""

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from pydantic import Field

from workspace_themes.contract import (
    APP_STYLES_DIRECTORY,
    CONTRACT_1_RENAMES,
    CONTRACT_VERSION,
    STYLE_FILE_SUFFIX,
    THEME_MANIFEST_FILENAME,
)

# What a contract-1 hover token's value reads as once hover is computed: the surface it shaded. A theme that set
# the hover to its surface (no shade) gets the same look from --state-ratio: 1.
HOVER_BASES: Final[Mapping[str, str]] = {
    "--c-accent-hover": "--c-accent-surface",
    "--c-danger-hover": "--c-danger-surface",
    "--c-stop-button-hover": "--c-inverse-surface",
    "--c-inverse-surface-hover": "--c-inverse-surface",
}

_TOKEN_PATTERN: Final = re.compile(
    r"(?<![\w-])("
    + "|".join(sorted(map(re.escape, CONTRACT_1_RENAMES), key=len, reverse=True))
    + r")(?![\w-])(\s*:[^;}]*;?)?"
)
_CONTRACT_LINE_PATTERN: Final = re.compile(
    r"^(\s*contract\s*=\s*)1(\s*(#.*)?)$", re.MULTILINE
)


# Contract 1's page color, and the content colors its apps drew their pages in. Contract 2's apps draw their pages
# in the page color, and the content colors became the raised surface (fields, cards, menus).
PAGE_TOKEN: Final = "--c-bg"
CONTENT_TOKENS: Final[tuple[str, ...]] = ("--c-surface", "--c-bg-chat")


class MigrationResult(FrozenModel):
    """A style file's text in contract 2's names, and the contract-1 declarations it read and dropped."""

    text: str = Field(description="The rewritten text")
    dropped: tuple[str, ...] = Field(
        description="The hover tokens whose declarations were removed"
    )
    declared: tuple[tuple[str, str], ...] = Field(
        default=(),
        description="Each contract-1 token the text declared, with its value",
    )


class FolderMigration(FrozenModel):
    """What moving a theme folder to contract 2 dropped, and what in it a person should look at."""

    dropped: tuple[str, ...] = Field(
        default=(), description="The hover tokens whose declarations were removed"
    )
    warnings: tuple[str, ...] = Field(
        default=(),
        description="Colors the theme set that contract 2 cannot keep as they were",
    )


@pure
def migrate_style_text(text: str) -> MigrationResult:
    """Renames every contract-1 token, declared or referenced. A declared hover token is replaced by a comment (its
    shade is computed now); a referenced one reads its surface."""
    dropped: list[str] = []
    declared: list[tuple[str, str]] = []

    def rewrite(match: re.Match[str]) -> str:
        name, declaration = match.group(1), match.group(2)
        renamed = CONTRACT_1_RENAMES[name]
        if declaration is not None:
            declared.append((name, _normalized_value(declaration)))
            if renamed is None:
                dropped.append(name)
                return f"/* {name}: contract 2 computes hover and active (--state-ratio sets the step) */"
            return renamed + declaration
        return renamed if renamed is not None else HOVER_BASES[name]

    migrated = _TOKEN_PATTERN.sub(rewrite, text)
    return MigrationResult(
        text=migrated, dropped=tuple(dropped), declared=tuple(declared)
    )


def _normalized_value(declaration: str) -> str:
    return declaration.strip().lstrip(":").rstrip(";").strip()


def _listed(names: Sequence[str], values: Mapping[str, Sequence[str]]) -> str:
    return ", ".join(f"{name} ({' / '.join(values[name])})" for name in names)


@pure
def find_merge_warnings(declared: Sequence[tuple[str, str]]) -> tuple[str, ...]:
    """The colors a contract-1 theme set that contract 2 cannot keep as they were: tokens that became one token with
    different colors, and a content color that no longer colors the app pages."""
    values: dict[str, list[str]] = {}
    for name, declared_value in declared:
        value = " ".join(declared_value.split()).lower()
        if value not in values.setdefault(name, []):
            values[name].append(value)
    by_target: dict[str, list[str]] = {}
    for name in values:
        target = CONTRACT_1_RENAMES[name]
        if target is not None:
            by_target.setdefault(target, []).append(name)
    warnings: list[str] = []
    for target, names in by_target.items():
        colors = {value for name in names for value in values[name]}
        if len(names) > 1 and len(colors) > 1:
            warnings.append(
                f"{_listed(names, values)} are one token in contract 2, {target}, which keeps the one declared "
                "last; check it is the color the theme should have"
            )
    page = values.get(PAGE_TOKEN, [])
    unlike_page = [
        name
        for name in CONTENT_TOKENS
        if any(value not in page for value in values.get(name, []))
    ]
    if unlike_page:
        page_note = (
            f"{PAGE_TOKEN} ({' / '.join(page)})"
            if page
            else f"{PAGE_TOKEN}, which the theme does not set"
        )
        warnings.append(
            f"app pages are drawn in the page color now, --c-body-surface (was {page_note}), so "
            f"{_listed(unlike_page, values)} now colors only fields, cards and menus (--c-body-surface-raised); "
            "set --c-body-surface to that color if the pages should keep it"
        )
    return tuple(warnings)


@pure
def migrate_manifest_text(text: str) -> str:
    return _CONTRACT_LINE_PATTERN.sub(rf"\g<1>{CONTRACT_VERSION}\g<2>", text, count=1)


def is_contract_1_theme(folder: Path) -> bool:
    manifest_path = folder / THEME_MANIFEST_FILENAME
    if not manifest_path.is_file():
        return False
    return (
        _CONTRACT_LINE_PATTERN.search(manifest_path.read_text(encoding="utf-8"))
        is not None
    )


def _style_files(folder: Path) -> list[Path]:
    files = sorted(folder.glob(f"*{STYLE_FILE_SUFFIX}"))
    files.extend(sorted((folder / APP_STYLES_DIRECTORY).glob(f"*{STYLE_FILE_SUFFIX}")))
    return [path for path in files if path.is_file()]


def migrate_theme_folder(folder: Path) -> FolderMigration:
    """Rewrites a contract-1 theme folder to contract 2 and says what it dropped and what to look at. A folder
    already on contract 2 is left alone, so running it again changes nothing."""
    if not is_contract_1_theme(folder):
        return FolderMigration()
    dropped: list[str] = []
    declared: list[tuple[str, str]] = []
    for path in _style_files(folder):
        original = path.read_text(encoding="utf-8")
        result = migrate_style_text(original)
        dropped.extend(result.dropped)
        declared.extend(result.declared)
        if result.text != original:
            path.write_text(result.text, encoding="utf-8")
    manifest_path = folder / THEME_MANIFEST_FILENAME
    manifest_path.write_text(
        migrate_manifest_text(manifest_path.read_text(encoding="utf-8")),
        encoding="utf-8",
    )
    return FolderMigration(
        dropped=tuple(dropped), warnings=find_merge_warnings(declared)
    )

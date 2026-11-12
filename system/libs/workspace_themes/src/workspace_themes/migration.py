"""Contract 1 to contract 2 (docs/system/blueprint/brand-design-system/): rewrites a theme folder's token names in
place, keeping everything else in its files as written."""

import re
from collections.abc import Mapping
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


class MigrationResult(FrozenModel):
    """A style file's text in contract 2's names, and the contract-1 declarations it dropped."""

    text: str = Field(description="The rewritten text")
    dropped: tuple[str, ...] = Field(
        description="The hover tokens whose declarations were removed"
    )


@pure
def migrate_style_text(text: str) -> MigrationResult:
    """Renames every contract-1 token, declared or referenced. A declared hover token is replaced by a comment (its
    shade is computed now); a referenced one reads its surface."""
    dropped: list[str] = []

    def rewrite(match: re.Match[str]) -> str:
        name, declaration = match.group(1), match.group(2)
        renamed = CONTRACT_1_RENAMES[name]
        if declaration is not None:
            if renamed is None:
                dropped.append(name)
                return f"/* {name}: contract 2 computes hover and active (--state-ratio sets the step) */"
            return renamed + declaration
        return renamed if renamed is not None else HOVER_BASES[name]

    return MigrationResult(
        text=_TOKEN_PATTERN.sub(rewrite, text), dropped=tuple(dropped)
    )


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


def migrate_theme_folder(folder: Path) -> tuple[str, ...]:
    """Rewrites a contract-1 theme folder to contract 2 and returns the hover tokens it dropped. A folder already
    on contract 2 is left alone, so running it again changes nothing."""
    if not is_contract_1_theme(folder):
        return ()
    dropped: list[str] = []
    for path in _style_files(folder):
        original = path.read_text(encoding="utf-8")
        result = migrate_style_text(original)
        dropped.extend(result.dropped)
        if result.text != original:
            path.write_text(result.text, encoding="utf-8")
    manifest_path = folder / THEME_MANIFEST_FILENAME
    manifest_path.write_text(
        migrate_manifest_text(manifest_path.read_text(encoding="utf-8")),
        encoding="utf-8",
    )
    return tuple(dropped)

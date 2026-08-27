from enum import auto
from typing import Final

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.pure import pure

from versioning.data_types import ChangeStats
from versioning.data_types import VersionKind

FILE_WEIGHT_LINES: Final[int] = 25


class MagnitudeTier(UpperCaseStrEnum):
    TINY = auto()
    SMALL = auto()
    MODERATE = auto()
    BIG = auto()
    SWEEPING = auto()


_TIER_CEILINGS: Final[tuple[tuple[float, MagnitudeTier], ...]] = (
    (60.0, MagnitudeTier.TINY),
    (250.0, MagnitudeTier.SMALL),
    (900.0, MagnitudeTier.MODERATE),
    (2500.0, MagnitudeTier.BIG),
)

_ADJECTIVE_BY_TIER: Final[dict[MagnitudeTier, str]] = {
    MagnitudeTier.TINY: "tiny",
    MagnitudeTier.SMALL: "small",
    MagnitudeTier.MODERATE: "moderate",
    MagnitudeTier.BIG: "large",
    MagnitudeTier.SWEEPING: "sweeping",
}

_NOUN_BY_KIND: Final[dict[VersionKind, str]] = {
    VersionKind.CHANGE: "update",
    VersionKind.FIX: "fix",
    VersionKind.HARDEN: "background tidy-up",
}

_UNKNOWN_KIND_NOUN: Final[str] = "change"


@pure
def change_magnitude(stats: ChangeStats) -> float:
    return float(stats.lines_changed + FILE_WEIGHT_LINES * stats.files_changed)


@pure
def change_tier(stats: ChangeStats) -> MagnitudeTier:
    magnitude = change_magnitude(stats)
    for ceiling, tier in _TIER_CEILINGS:
        if magnitude < ceiling:
            return tier
    return MagnitudeTier.SWEEPING


@pure
def size_adjective(stats: ChangeStats) -> str:
    return _ADJECTIVE_BY_TIER[change_tier(stats)]


@pure
def version_phrase(kind: VersionKind | None, stats: ChangeStats | None) -> str | None:
    """One noun phrase saying what a version is, or None when its name already says it."""
    if kind is VersionKind.RESTORE:
        return None
    if kind is VersionKind.BUILD:
        return "The first build"
    if kind is VersionKind.PORT:
        return "Brought back from an earlier version"
    if stats is None:
        return None
    noun = _NOUN_BY_KIND[kind] if kind is not None else _UNKNOWN_KIND_NOUN
    return f"A {size_adjective(stats)} {noun}"

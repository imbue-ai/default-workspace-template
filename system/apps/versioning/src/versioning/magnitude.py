"""Deterministic sizing of a version's change, and the one phrase that describes it.

The timeline never shows files to the user; instead each version's change size
is told twice from the same measurement -- once visually (the dot's diameter)
and once in words (the version phrase). Both derive from one shared tier, so
they can never disagree: a smaller-tier dot is always visibly smaller than a
bigger-tier one. Five discrete tiers give the feed real visual variance while
staying a clean array instead of a smear of near-identical circles.

The phrase folds the size and the kind of change into a single noun phrase --
"A small fix", "A large update" -- rather than showing them as two labels. Two
labels read as a size followed by a bare kind ("A big change - Change"), which
is both repetitive and easy to mistake for a button, since the kind names are
spelled like verbs.
"""

from enum import auto
from typing import Final

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.pure import pure

from versioning.data_types import ChangeStats
from versioning.data_types import VersionKind

# A file counts this many lines on top of its actual line churn, so a change
# spread across many files reads bigger than the same churn in one file.
FILE_WEIGHT_LINES: Final[int] = 25


class MagnitudeTier(UpperCaseStrEnum):
    """The shared size tier behind both the dot diameter and the size sentence."""

    TINY = auto()
    SMALL = auto()
    MODERATE = auto()
    BIG = auto()
    SWEEPING = auto()


# Tier upper bounds on the magnitude scalar, in ascending tier order; the last
# tier is unbounded. Chosen so everyday app changes spread across the middle.
_TIER_CEILINGS: Final[tuple[tuple[float, MagnitudeTier], ...]] = (
    (60.0, MagnitudeTier.TINY),
    (250.0, MagnitudeTier.SMALL),
    (900.0, MagnitudeTier.MODERATE),
    (2500.0, MagnitudeTier.BIG),
)

_DIAMETER_PX_BY_TIER: Final[dict[MagnitudeTier, float]] = {
    MagnitudeTier.TINY: 8.0,
    MagnitudeTier.SMALL: 13.0,
    MagnitudeTier.MODERATE: 18.0,
    MagnitudeTier.BIG: 24.0,
    MagnitudeTier.SWEEPING: 30.0,
}

_ADJECTIVE_BY_TIER: Final[dict[MagnitudeTier, str]] = {
    MagnitudeTier.TINY: "tiny",
    MagnitudeTier.SMALL: "small",
    MagnitudeTier.MODERATE: "moderate",
    MagnitudeTier.BIG: "large",
    MagnitudeTier.SWEEPING: "sweeping",
}

# What the version *is*, as a noun. Deliberately not the enum's own spelling:
# "change" and "fix" name a kind but also an action, and a bare kind word next
# to a size reads like a control rather than a description.
_NOUN_BY_KIND: Final[dict[VersionKind, str]] = {
    VersionKind.CHANGE: "update",
    VersionKind.FIX: "fix",
    VersionKind.HARDEN: "background tidy-up",
}

# The noun for a version whose commit recorded no kind at all -- everything from
# before the trailer convention.
_UNKNOWN_KIND_NOUN: Final[str] = "change"

DOT_MIN_DIAMETER_PX: Final[float] = _DIAMETER_PX_BY_TIER[MagnitudeTier.TINY]
DOT_MAX_DIAMETER_PX: Final[float] = _DIAMETER_PX_BY_TIER[MagnitudeTier.SWEEPING]


@pure
def change_magnitude(stats: ChangeStats) -> float:
    """One scalar for how big a change was: line churn plus a per-file weight."""
    return float(stats.lines_changed + FILE_WEIGHT_LINES * stats.files_changed)


@pure
def change_tier(stats: ChangeStats) -> MagnitudeTier:
    magnitude = change_magnitude(stats)
    for ceiling, tier in _TIER_CEILINGS:
        if magnitude < ceiling:
            return tier
    return MagnitudeTier.SWEEPING


@pure
def dot_diameter_px(stats: ChangeStats) -> float:
    """Dot diameter for a version; one distinct size per tier."""
    return _DIAMETER_PX_BY_TIER[change_tier(stats)]


@pure
def size_adjective(stats: ChangeStats) -> str:
    """The plain-language size word; always agrees with the dot's size."""
    return _ADJECTIVE_BY_TIER[change_tier(stats)]


@pure
def version_phrase(kind: VersionKind | None, stats: ChangeStats | None) -> str | None:
    """One noun phrase saying what a version is, or None when its name already says it.

    A restore's own name is "Restored from ...", so a phrase underneath it would
    only repeat it. A version whose diff could not be measured has no size to
    report, and the first build's size says nothing worth reading.
    """
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

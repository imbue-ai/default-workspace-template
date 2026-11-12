"""A skill's exemption from named checks: reading it from frontmatter and writing it back.

An exemption is two strings in a SKILL.md's frontmatter ``metadata``:

    metadata:
      exempt-checks: size depth
      exempt-reason: awaiting-restructure

``docs/system/skill_authoring_guide.md`` says what each reason means and who may write it.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import yaml

from agents_shared.skill_checks import Check

EXEMPT_CHECKS_KEY = "exempt-checks"
EXEMPT_REASON_KEY = "exempt-reason"

_METADATA_KEY_LINE = re.compile(r"^metadata\s*:")


class ExemptionReason(enum.StrEnum):
    # The skill existed before the checks did. Written only by a flow that brings existing
    # skills under the checks: an update, a migration, adopting a template.
    PREDATES_CHECKS = "predates-checks"
    # A built-in skill whose restructure is scheduled. Written only by the template's maintainers.
    AWAITING_RESTRUCTURE = "awaiting-restructure"


class ExemptionAllowance(enum.Enum):
    """Which exemptions a validation honours, by what is being validated."""

    # A skill as it stands: whatever exemption it carries.
    STANDING_SKILL = enum.auto()
    # A skill being edited. Its own exemption, written when it came under the checks, has to
    # go with the edit. A built-in's waits for the release that restructures it.
    EDITED_SKILL = enum.auto()
    # A skill being created: none.
    NEW_SKILL = enum.auto()

    def honours(self, reason: ExemptionReason) -> bool:
        match self:
            case ExemptionAllowance.STANDING_SKILL:
                return True
            case ExemptionAllowance.EDITED_SKILL:
                return reason is ExemptionReason.AWAITING_RESTRUCTURE
            case ExemptionAllowance.NEW_SKILL:
                return False


@dataclass(frozen=True)
class Exemption:
    checks: frozenset[Check]
    reason: ExemptionReason

    def checks_text(self) -> str:
        return " ".join(check.value for check in Check if check in self.checks)


def read_exemption(frontmatter: Mapping[str, Any]) -> Exemption | None:
    """The exemption a SKILL.md's frontmatter carries, or ``None``.

    Raises ``ValueError`` for one that names an unknown check or reason, or is half written.
    """
    metadata = frontmatter.get("metadata")
    if not isinstance(metadata, Mapping):
        return None
    checks_text = metadata.get(EXEMPT_CHECKS_KEY)
    reason_text = metadata.get(EXEMPT_REASON_KEY)
    if checks_text is None and reason_text is None:
        return None
    if not isinstance(checks_text, str) or not isinstance(reason_text, str):
        raise ValueError(
            f"frontmatter.metadata must carry `{EXEMPT_CHECKS_KEY}` and `{EXEMPT_REASON_KEY}` "
            "together, each as one string, or neither"
        )
    names = checks_text.replace(",", " ").split()
    known_checks = {check.value for check in Check}
    unknown = sorted(set(names) - known_checks)
    if unknown or not names:
        raise ValueError(
            f"frontmatter.metadata.{EXEMPT_CHECKS_KEY} ({checks_text!r}) must name one or more "
            f"of: {', '.join(sorted(known_checks))}"
        )
    known_reasons = {reason.value for reason in ExemptionReason}
    if reason_text not in known_reasons:
        raise ValueError(
            f"frontmatter.metadata.{EXEMPT_REASON_KEY} ({reason_text!r}) must be one of: "
            f"{', '.join(sorted(known_reasons))}"
        )
    return Exemption(
        checks=frozenset(Check(name) for name in names),
        reason=ExemptionReason(reason_text),
    )


def with_exemption(skill_md_text: str, exemption: Exemption | None) -> str:
    """``skill_md_text`` with its exemption set to ``exemption``, or removed for ``None``.

    Only the frontmatter's ``metadata`` block is rewritten; every other byte is kept.
    Raises ``ValueError`` when the text has no frontmatter to hold one.
    """
    lines = skill_md_text.splitlines(keepends=True)
    stripped = [line.rstrip("\r\n") for line in lines]
    if not stripped or stripped[0].strip() != "---" or "---" not in stripped[1:]:
        raise ValueError("SKILL.md has no `---` frontmatter to hold an exemption")
    end = stripped.index("---", 1)
    frontmatter = yaml.safe_load("\n".join(stripped[1:end]))
    if not isinstance(frontmatter, dict):
        raise ValueError("SKILL.md frontmatter must be a YAML mapping")

    existing = frontmatter.get("metadata")
    metadata = dict(existing) if isinstance(existing, dict) else {}
    metadata.pop(EXEMPT_CHECKS_KEY, None)
    metadata.pop(EXEMPT_REASON_KEY, None)
    if exemption is not None:
        metadata[EXEMPT_CHECKS_KEY] = exemption.checks_text()
        metadata[EXEMPT_REASON_KEY] = exemption.reason.value
    block = (
        yaml.safe_dump(
            {"metadata": metadata},
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
            width=10**6,
        ).splitlines(keepends=True)
        if metadata
        else []
    )

    start = next(
        (index for index in range(1, end) if _METADATA_KEY_LINE.match(stripped[index])),
        None,
    )
    if start is None:
        return "".join([*lines[:end], *block, *lines[end:]])
    stop = start + 1
    while stop < end and (not stripped[stop].strip() or stripped[stop][0] in " \t"):
        stop += 1
    # Blank lines after the block belong to the frontmatter's layout, not to metadata.
    while stop > start + 1 and not stripped[stop - 1].strip():
        stop -= 1
    return "".join([*lines[:start], *block, *lines[stop:]])

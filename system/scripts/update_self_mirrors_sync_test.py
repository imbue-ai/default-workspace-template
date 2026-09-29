"""The scripts update-self keeps its own copy of must stay byte-identical to the canonical ones.

update-self stages its skill directory from the release it updates to and runs it as a
self-contained unit, so it cannot import or run anything out of ``system/scripts/`` of a
workspace that may predate it; the scripts it shares are mirrored into its own directory
instead, and held byte-identical here rather than equivalent by review.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).parent
_UPDATE_SELF_SCRIPTS_DIR = (
    Path(__file__).parents[2] / ".agents" / "skills" / "update-self" / "scripts"
)


@pytest.mark.parametrize("name", ["tool_env.py", "run_in_background.py"])
def test_update_selfs_copy_matches_the_canonical_one(name: str) -> None:
    canonical = _SCRIPTS_DIR / name
    mirrored = _UPDATE_SELF_SCRIPTS_DIR / name
    assert mirrored.is_file(), f"{mirrored} is missing; copy {canonical} to it"
    assert mirrored.read_bytes() == canonical.read_bytes(), (
        f"{mirrored} has drifted from {canonical}. Edit one and copy it over the other; "
        "they are two halves of one implementation, not two implementations."
    )

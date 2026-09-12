"""The two copies of ``tool_env.py`` must stay byte-identical.

The update apply is staged and run as a self-contained unit, so it cannot import out of
``system/scripts/``; the logic it shares with the build is vendored into its own directory
instead. Equivalence by review is what failed last time -- the two shebang parsers drifted
and the difference only showed on a ``#! /path`` spelling -- so this asserts sameness,
which cannot drift at all.
"""

from __future__ import annotations

from pathlib import Path

_CANONICAL = Path(__file__).with_name("tool_env.py")
_VENDORED = (
    Path(__file__).parents[2]
    / ".agents"
    / "skills"
    / "update-self"
    / "scripts"
    / "tool_env.py"
)


def test_the_vendored_copy_matches_the_canonical_one() -> None:
    assert _VENDORED.is_file(), f"{_VENDORED} is missing; copy {_CANONICAL} to it"
    assert _VENDORED.read_bytes() == _CANONICAL.read_bytes(), (
        f"{_VENDORED} has drifted from {_CANONICAL}. Edit one and copy it over the other; "
        "they are two halves of one implementation, not two implementations."
    )

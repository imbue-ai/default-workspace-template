"""How the workspace's objects get their names.

The naming scheme mirrors how the minds app names hosts: every object wears a
human-readable display name ("Chat 2"), and its true name -- the identifier
embedded in tmux sessions, paths, and refs -- is a deterministic canonical form
of that display name ("Chat-2"). The pair is established at create time and
kept matched on rename, so no surface ever shows a machine-minted coolname.

For chat agents the display name lives on the mngr agent itself, as its
``display_name`` label, with the canonical form as the agent's mngr name.
Display names are minted here, server-side, as the first free "<word> N" for
the harness's word ("Chat 1", "Codex 2", ...), so two clients creating at the
same time cannot both mint "Chat 1".
"""

from collections.abc import Iterable
from typing import Final

from app_instances.primitives import canonical_name_from_title
from app_instances.primitives import is_name_conflict as is_title_conflict

from imbue.imbue_common.pure import pure
from imbue.system_interface.harnesses.harness_type import HarnessType

# The word a harness's auto-minted chat names count under: a Codex chat is
# "Codex 1", not "Chat 2", so the fleets number independently and the name says
# which harness is behind it. Mirrored by the frontend's tab icons; the names
# themselves are minted only here.
AUTO_NAME_WORD_BY_HARNESS: Final[dict[HarnessType, str]] = {
    HarnessType.CLAUDE: "Chat",
    HarnessType.CODEX: "Codex",
    HarnessType.PI_CODING: "Pi",
    HarnessType.OPENCODE: "OpenCode",
    HarnessType.ANTIGRAVITY: "Agy",
}


@pure
def canonical_agent_name(name: str) -> str:
    """The true-name form of a human-readable chat name ("Chat 2" -> "Chat-2").

    The workspace app model's naming rule, shared with every app through the
    instances library. It mirrors mngr's own canonicalization rather than
    importing it: a workspace's vendored mngr may predate free-form names, and
    passing it a name it would reject fails the create outright. Sending the
    canonical name (plus the typed one as a ``display_name`` label) is accepted
    by every mngr version. Returns "" when nothing usable remains (e.g. the
    input was all emoji).
    """
    return canonical_name_from_title(name)


@pure
def _canonical_name_key(name: str) -> str:
    """The collision key for a name: its canonical form, case-insensitively.

    Two names conflict exactly when their true names would collide in mngr, so
    the comparison happens on canonical forms; the casefold makes the check
    strictly stronger, refusing near-duplicates ("chat 2" vs "Chat 2") that
    would only confuse.
    """
    return canonical_agent_name(name).casefold()


@pure
def first_free_numbered_name(word: str, taken_names: Iterable[str]) -> str:
    """The first free "<word> N" display name: "Chat 1", "Codex 2", ...

    "First free" fills gaps -- destroying "Chat 1" frees the slot for the next
    create. ``taken_names`` is every name already in use on the machine: agent
    display names, agent true names, in-flight creates, and every chosen member
    title, so a terminal someone renamed to "Chat 2" blocks that slot too.
    Names are compared by their canonical forms (case-insensitively), which is
    the collision rule mngr itself enforces on the true names.
    """
    taken_keys = {_canonical_name_key(name) for name in taken_names}
    n = 1
    while _canonical_name_key(f"{word} {n}") in taken_keys:
        n += 1
    return f"{word} {n}"


@pure
def is_name_conflict(candidate_name: str, taken_names: Iterable[str]) -> bool:
    """Whether ``candidate_name`` collides with any taken name.

    Same rule as :func:`first_free_numbered_name`: canonical forms, compared
    case-insensitively.
    """
    return is_title_conflict(candidate_name, taken_names)

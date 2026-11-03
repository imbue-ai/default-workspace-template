"""The roster (the multiplayer spec, section 4.2): everyone who may be in the workspace, as the shell answers it and
the apps read it, with the handle each is mentioned by and the colour each is drawn in."""

import hashlib
import re
from collections.abc import Sequence
from typing import Final

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from pydantic import Field

from workspace_layout.primitives import ParticipantKey
from workspace_layout.primitives import ParticipantKind

# A handle is the display name with runs of whitespace as one hyphen and anything else outside this alphabet dropped.
_HANDLE_ALPHABET: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9._-]")
_WHITESPACE_RUN: Final[re.Pattern[str]] = re.compile(r"\s+")
# What a name with nothing usable in it is mentioned by.
_FALLBACK_HANDLE: Final[str] = "someone"

# The eight colours a participant may be drawn in (the viewing chrome, the tray ring, the card), chosen by hashing the
# key so every surface agrees; ``workspace_ui``'s tokens carry the same eight.
PARTICIPANT_COLORS: Final[tuple[str, ...]] = (
    "#2f6b4f",
    "#3b5ba9",
    "#a3473b",
    "#8a5a2b",
    "#6a3d9a",
    "#1f7a8c",
    "#b0366d",
    "#4f6b2f",
)


class RosterParticipant(FrozenModel):
    """One entry of the roster: a person, an invited email, a visiting agent, or the workspace's agent."""

    key: ParticipantKey = Field(description="The participant's key")
    kind: ParticipantKind = Field(description="What the participant is")
    display_name: str = Field(description="What to call them: the profile's name, else the email, else the grant's")
    profile_picture_url: str | None = Field(description="Their picture, when there is one")
    email: str | None = Field(description="Their email, when known")
    handle: str = Field(description="What they are mentioned by, without the @")
    color: str = Field(description="The colour they are drawn in, as '#RRGGBB'")
    is_owner: bool = Field(description="Whether this is the workspace's owner")
    is_connected: bool = Field(description="Whether they are here right now")
    last_seen: str | None = Field(description="When they were last here (ISO 8601), when ever")
    last_input_at: str | None = Field(
        description="When their newest page last saw keyboard or pointer input (ISO 8601), when the page reports it"
    )


class RosterDocument(FrozenModel):
    """The whole roster (``GET /api/roster`` and the ``roster_updated`` push), in display order."""

    participants: tuple[RosterParticipant, ...] = Field(description="Everyone who may be here, in roster order")


@pure
def handle_of_name(name: str) -> str:
    """The handle a display name is mentioned by (the multiplayer spec, section 7.1)."""
    hyphenated = _WHITESPACE_RUN.sub("-", name.strip())
    handle = _HANDLE_ALPHABET.sub("", hyphenated)
    return handle if handle else _FALLBACK_HANDLE


@pure
def assign_handles(names: Sequence[str], taken: Sequence[str] = ()) -> list[str]:
    """One handle per name, in order, a later collision (case-insensitively, with ``taken`` too) suffixed ``-2``,
    ``-3``, and so on."""
    used = {handle.lower() for handle in taken}
    handles: list[str] = []
    for name in names:
        base = handle_of_name(name)
        candidate = base
        suffix = 2
        while candidate.lower() in used:
            candidate = f"{base}-{suffix}"
            suffix += 1
        used.add(candidate.lower())
        handles.append(candidate)
    return handles


@pure
def participant_color(key: ParticipantKey) -> str:
    """The colour a participant is drawn in: one of the eight, by the key's hash."""
    digest = hashlib.sha256(str(key).encode("utf-8")).digest()
    return PARTICIPANT_COLORS[digest[0] % len(PARTICIPANT_COLORS)]

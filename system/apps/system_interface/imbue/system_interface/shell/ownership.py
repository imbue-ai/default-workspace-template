"""Whose a desktop is (the multiplayer spec, section 8.1): the rules over a ``Desktop``'s ``owner`` and a request's
identity that the routes and the agent ops share. A desktop with no owner is the workspace owner's, under either
of the owner's keys; any other is its owner's alone. Pure functions, kept apart from the desktop editor so that one
stays a function of the records alone.
"""

from collections.abc import Sequence

from workspace_layout.identity import RequestIdentity
from workspace_layout.identity import is_owner_participant
from workspace_layout.identity import participant_key_of
from workspace_layout.primitives import OWNER_PARTICIPANT_KEY
from workspace_layout.primitives import DesktopId
from workspace_layout.primitives import ParticipantKey
from workspace_layout.records import ClientRecord
from workspace_layout.records import Desktop

from imbue.imbue_common.pure import pure
from imbue.system_interface.shell.errors import DesktopValueError


@pure
def is_desktop_owned_by(desktop: Desktop, identity: RequestIdentity) -> bool:
    """Whether the desktop is the requester's own (the multiplayer spec, section 8.1): a desktop with no owner is
    the workspace owner's, under either of the owner's keys, and any other is its owner's alone."""
    if desktop.owner is None:
        return is_owner_participant(identity)
    return desktop.owner == participant_key_of(identity)


@pure
def desktop_owner_keys(desktop: Desktop, workspace_owner_key: ParticipantKey) -> frozenset[ParticipantKey]:
    """The participant keys the desktop owner's clients may be recorded under: both of the workspace owner's for a
    desktop with no owner (their clients carry the synthetic ``owner`` key until the workspace is shared), the one
    key otherwise."""
    if desktop.owner is None or desktop.owner in (workspace_owner_key, OWNER_PARTICIPANT_KEY):
        return frozenset((workspace_owner_key, OWNER_PARTICIPANT_KEY))
    return frozenset((desktop.owner,))


@pure
def owners_clients_newest_first(
    desktop: Desktop, workspace_owner_key: ParticipantKey, clients: Sequence[ClientRecord]
) -> tuple[ClientRecord, ...]:
    """The desktop owner's clients, the most recently seen first: whose layout a viewer of the desktop is shown
    (the multiplayer spec, section 8.3)."""
    keys = desktop_owner_keys(desktop, workspace_owner_key)
    owned = [client for client in clients if client.participant in keys]
    return tuple(sorted(owned, key=lambda client: client.last_seen, reverse=True))


@pure
def desktop_owner_of(identity: RequestIdentity) -> ParticipantKey | None:
    """The ``owner`` a desktop the requester creates is written with: none for the workspace owner (whose desktops
    carry no key, as every desktop from before the key was kept), else the requester's key."""
    return None if is_owner_participant(identity) else participant_key_of(identity)


@pure
def reordered_desktops(
    desktops: Sequence[Desktop], ordered_own_ids: Sequence[DesktopId], identity: RequestIdentity
) -> tuple[Desktop, ...]:
    """The desktops with the requester's own in the order ``ordered_own_ids`` gives, which must name exactly those
    (raises DesktopValueError otherwise); every other desktop keeps its place (the multiplayer spec, section 8.2)."""
    own_by_id = {desktop.id: desktop for desktop in desktops if is_desktop_owned_by(desktop, identity)}
    if len(set(ordered_own_ids)) != len(ordered_own_ids) or set(ordered_own_ids) != set(own_by_id):
        raise DesktopValueError(
            f"The order must name each of your desktops once ({', '.join(sorted(own_by_id))}), "
            f"not {', '.join(ordered_own_ids)}"
        )
    ordered_own = iter(ordered_own_ids)
    return tuple(own_by_id[next(ordered_own)] if desktop.id in own_by_id else desktop for desktop in desktops)

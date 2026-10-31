"""Resolve an account-keyed setting for a request identity, by the user-profile-state spec's order.

Earliest wins: the requesting user's own override in this workspace, then (for the owner) the
shell's unsent account changes, then (for the owner) the owner account's value from the
snapshot, then the given default. A visitor has no account value in this workspace, so a
visitor resolves from their override and the default alone.

Owner detection follows the identity header exactly as the shell does: a request is the
owner's when the header says so, and the owner's user id is then the snapshot's owner id --
which is what lets an unshared workspace, whose header carries no user id, still key the
owner's overrides. A visitor's user id comes from the header.
"""

from pathlib import Path

from imbue.imbue_common.pure import pure

from workspace_profile.identity import RequestIdentity
from workspace_profile.overrides import (
    load_unsent_change_values,
    load_user_override_values,
)
from workspace_profile.snapshot import LOCAL_ACCOUNT_ID, WorkspaceProfileSnapshot


@pure
def resolve_request_user_id(
    identity: RequestIdentity, snapshot: WorkspaceProfileSnapshot | None
) -> str | None:
    """The user id whose overrides this request resolves with.

    The owner's is the snapshot's owner id (``local`` when no client has written one yet), so
    an unshared workspace's owner, whose header carries no user id, still keys their own
    overrides. A visitor's comes from the header; a visitor without one (which the broker
    should make impossible) keys nobody's.
    """
    if identity.owner:
        return snapshot.owner_user_id if snapshot is not None else LOCAL_ACCOUNT_ID
    return identity.user_id


def resolve_profile_setting(
    key: str,
    default: object,
    *,
    snapshot: WorkspaceProfileSnapshot | None,
    identity: RequestIdentity,
    workspace_root: Path,
) -> object:
    """The setting's value for this request: override, then (owner only) unsent change, then snapshot, then default."""
    user_id = resolve_request_user_id(identity, snapshot)
    if user_id is not None:
        override_by_key = load_user_override_values(workspace_root, user_id)
        if key in override_by_key:
            return override_by_key[key]
    if identity.owner:
        unsent_by_key = load_unsent_change_values(workspace_root)
        if key in unsent_by_key:
            return unsent_by_key[key]
        if snapshot is not None and key in snapshot.value_by_key:
            return snapshot.value_by_key[key]
    return default

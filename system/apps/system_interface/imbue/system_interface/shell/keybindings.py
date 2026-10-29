"""The shell's keybindings: its own manifest, the catalog of every app's, and the profile-file writes.

The Keyboard settings lists every built-in app's actions from the manifests their backends serve
(``GET <api base>/keybindings/actions``, the api base read off each registry row's schema path),
resolved for the requesting identity by the ``workspace_profile`` library. Saves land in the
profile files that library owns: a visitor's (or the owner's "only in this workspace") choice in
the user's override file, and the owner's "everywhere" choice in the unsent file plus the owner's
override file, so the workspace behaves as asked at once while the delivery to the account runs
(``keybindings_delivery.py``).
"""

import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import httpx
from loguru import logger
from pydantic import Field
from pydantic import ValidationError
from workspace_profile.identity import RequestIdentity
from workspace_profile.keybindings import keybinding_assignment
from workspace_profile.keybindings import with_keybinding_assignment
from workspace_profile.keybindings import without_keybinding_assignment
from workspace_profile.manifest import KEYBINDINGS_ACTIONS_SUBPATH
from workspace_profile.manifest import KeybindingActionDeclaration
from workspace_profile.manifest import KeybindingsActionsResponse
from workspace_profile.overrides import load_unsent_change_values
from workspace_profile.overrides import load_user_override_values
from workspace_profile.overrides import write_unsent_change_values
from workspace_profile.overrides import write_user_override_values
from workspace_profile.resolve import resolve_request_user_id
from workspace_profile.snapshot import WorkspaceProfileSnapshot

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.system_interface.shell.data_types import AppInventoryEntry

SHELL_APP_NAME: Final[str] = "system_interface"

SHELL_KEYBINDINGS_MANIFEST: Final[KeybindingsActionsResponse] = KeybindingsActionsResponse(
    app=SHELL_APP_NAME,
    actions=(
        KeybindingActionDeclaration(
            id="launcher-secondary-send",
            label="Launcher: send the typed text to a chosen chat",
            default_chords=("ctrl+enter", "meta+enter"),
        ),
    ),
)

# One lock for every read-modify-write of the profile's override and unsent files, the way
# STATE_FILES_LOCK serializes the shell's own state files.
PROFILE_FILES_LOCK: Final[threading.RLock] = threading.RLock()

_MANIFEST_FETCH_TIMEOUT_SECONDS: Final[float] = 3.0

# Where a resolved chord assignment came from, for the Keyboard settings' badges.
SOURCE_DEFAULT: Final[str] = "default"
SOURCE_ACCOUNT: Final[str] = "account"
SOURCE_UNSENT: Final[str] = "unsent"
SOURCE_WORKSPACE: Final[str] = "workspace"


class AppKeybindingsManifest(FrozenModel):
    """One app's manifest with what the settings screen shows for the app itself."""

    display_name: str = Field(description="The app's name as users see it")
    manifest: KeybindingsActionsResponse = Field(description="The declared actions")


def _api_base_of(entry: AppInventoryEntry) -> str | None:
    schema_path = entry.row.api_schema_path
    if schema_path is None or not str(schema_path).endswith("/schema"):
        return None
    return str(entry.row.url).rstrip("/") + str(schema_path).removesuffix("/schema")


def fetch_app_keybindings_manifests(entries: Sequence[AppInventoryEntry]) -> list[AppKeybindingsManifest]:
    """Every app's keybindings manifest: the shell's own, then each registered app that serves one.

    An app that is down, serves no such route, or answers something unreadable is skipped
    (logged): its actions reappear in the catalog when it answers again.
    """
    manifests = [AppKeybindingsManifest(display_name="Workspace", manifest=SHELL_KEYBINDINGS_MANIFEST)]
    for entry in entries:
        if str(entry.row.name) == SHELL_APP_NAME:
            continue
        api_base = _api_base_of(entry)
        if api_base is None:
            continue
        try:
            response = httpx.get(f"{api_base}{KEYBINDINGS_ACTIONS_SUBPATH}", timeout=_MANIFEST_FETCH_TIMEOUT_SECONDS)
        except httpx.HTTPError as e:
            logger.debug("Skipped {}'s keybindings manifest: {}", entry.row.name, e)
            continue
        if response.status_code != 200:
            logger.debug("Skipped {}'s keybindings manifest: HTTP {}", entry.row.name, response.status_code)
            continue
        try:
            manifest = KeybindingsActionsResponse.model_validate(response.json())
        except (ValueError, ValidationError) as e:
            logger.warning("Ignored {}'s unreadable keybindings manifest: {}", entry.row.name, e)
            continue
        display_name = str(entry.row.display_name) if entry.row.display_name is not None else str(entry.row.name)
        manifests.append(AppKeybindingsManifest(display_name=display_name, manifest=manifest))
    return manifests


def resolve_assignment_source(
    app_name: str,
    action_id: str,
    *,
    snapshot: WorkspaceProfileSnapshot | None,
    identity: RequestIdentity,
    workspace_root: Path,
) -> str:
    """Which layer answers the action for this identity, by the resolution's own order."""
    user_id = resolve_request_user_id(identity, snapshot)
    if user_id is not None:
        if keybinding_assignment(load_user_override_values(workspace_root, user_id), app_name, action_id) is not None:
            return SOURCE_WORKSPACE
    if identity.owner:
        if keybinding_assignment(load_unsent_change_values(workspace_root), app_name, action_id) is not None:
            return SOURCE_UNSENT
        if snapshot is not None and keybinding_assignment(snapshot.value_by_key, app_name, action_id) is not None:
            return SOURCE_ACCOUNT
    return SOURCE_DEFAULT


def save_user_keybinding(
    workspace_root: Path, user_id: str, app_name: str, action_id: str, chords: Sequence[str]
) -> None:
    """Set one assignment in the user's own override file (the "only in this workspace" save)."""
    with PROFILE_FILES_LOCK:
        values = load_user_override_values(workspace_root, user_id)
        write_user_override_values(
            workspace_root, user_id, with_keybinding_assignment(values, app_name, action_id, chords)
        )


def remove_user_keybinding(workspace_root: Path, user_id: str, app_name: str, action_id: str) -> None:
    """Drop one assignment from the user's override file, so the account value (or default) answers again."""
    with PROFILE_FILES_LOCK:
        values = load_user_override_values(workspace_root, user_id)
        write_user_override_values(workspace_root, user_id, without_keybinding_assignment(values, app_name, action_id))


def save_unsent_keybinding(
    workspace_root: Path, owner_user_id: str, app_name: str, action_id: str, chords: Sequence[str]
) -> None:
    """Record one "everywhere" save: the unsent file plus the owner's override file (spec section 9.3),
    so the workspace behaves as asked at once however the delivery goes."""
    with PROFILE_FILES_LOCK:
        unsent = load_unsent_change_values(workspace_root)
        write_unsent_change_values(workspace_root, with_keybinding_assignment(unsent, app_name, action_id, chords))
        overrides = load_user_override_values(workspace_root, owner_user_id)
        write_user_override_values(
            workspace_root, owner_user_id, with_keybinding_assignment(overrides, app_name, action_id, chords)
        )


def discard_unsent_keybinding(workspace_root: Path, owner_user_id: str, app_name: str, action_id: str) -> None:
    """Drop one undelivered "everywhere" save from the unsent file and the owner's override file."""
    with PROFILE_FILES_LOCK:
        unsent = load_unsent_change_values(workspace_root)
        write_unsent_change_values(workspace_root, without_keybinding_assignment(unsent, app_name, action_id))
        overrides = load_user_override_values(workspace_root, owner_user_id)
        write_user_override_values(
            workspace_root, owner_user_id, without_keybinding_assignment(overrides, app_name, action_id)
        )


def clear_delivered_keybindings(
    workspace_root: Path, owner_user_id: str, delivered: dict[str, dict[str, list[str]]]
) -> None:
    """Drop the delivered assignments from the unsent file, and from the owner's override file where the
    override still carries exactly the delivered value (a different value there is a deliberate
    "only in this workspace" choice made since, and stays)."""
    with PROFILE_FILES_LOCK:
        unsent = load_unsent_change_values(workspace_root)
        overrides = load_user_override_values(workspace_root, owner_user_id)
        for app_name, chords_by_action in delivered.items():
            for action_id, chords in chords_by_action.items():
                if keybinding_assignment(unsent, app_name, action_id) == list(chords):
                    unsent = without_keybinding_assignment(unsent, app_name, action_id)
                if keybinding_assignment(overrides, app_name, action_id) == list(chords):
                    overrides = without_keybinding_assignment(overrides, app_name, action_id)
        write_unsent_change_values(workspace_root, unsent)
        write_user_override_values(workspace_root, owner_user_id, overrides)

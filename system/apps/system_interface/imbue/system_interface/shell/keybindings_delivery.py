"""Delivering the owner's "everywhere" keybinding saves to the account (user-profile-state spec, section 9.3).

The shell PUTs the account's full keybindings value -- the snapshot's, overlaid with the unsent
saves -- to the Imbue Studio desktop through this machine's latchkey gateway
(``/minds-api-proxy/api/v1/workspaces/<this workspace>/account-settings/keybindings``). Nothing
is pre-granted: the first call 403s, and the save flow files one ``workspace`` permission request
naming the ``minds-workspaces-account-settings`` verb and this workspace; the desktop's inbox
resolution reaches the page as ``minds:permission-resolutions``, whose arrival retries the
delivery. A denial or an unreachable desktop leaves the saves in the unsent file (and the owner's
override file), retried whenever a save, a resolution, or the Keyboard settings asks again, until
one lands or the user discards them.
"""

import json
import os
from enum import auto
from pathlib import Path
from typing import Final

import httpx
from loguru import logger
from pydantic import Field
from workspace_profile.keybindings import KEYBINDINGS_SETTING_KEY
from workspace_profile.keybindings import parse_keybindings_value
from workspace_profile.overrides import load_unsent_change_values
from workspace_profile.snapshot import load_workspace_profile_snapshot

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.system_interface.shell.gateway_access import GatewayAccess
from imbue.system_interface.shell.keybindings import clear_delivered_keybindings
from imbue.system_interface.shell.state_files import STATE_FILES_LOCK
from imbue.system_interface.shell.state_files import read_json_object
from imbue.system_interface.shell.state_files import write_json_atomic

ACCOUNT_SETTINGS_VERB: Final[str] = "minds-workspaces-account-settings"

# The agent-environment variable naming this workspace's own agent id, which is both the
# cross-workspace route's target and the permission file the grant splices into.
ENV_AGENT_ID: Final[str] = "MNGR_AGENT_ID"

_DELIVERY_STATE_FILENAME: Final[str] = "keybindings_delivery.json"
_DELIVERY_STATE_VERSION: Final[int] = 1
_DELIVERY_TIMEOUT_SECONDS: Final[float] = 10.0

_PERMISSION_RATIONALE: Final[str] = (
    "Save this workspace's keyboard shortcut changes to your account, so they apply in every workspace."
)


class KeybindingsDeliveryStatus(UpperCaseStrEnum):
    """How one delivery attempt ended."""

    DELIVERED = auto()
    NOTHING_TO_DELIVER = auto()
    PERMISSION_PENDING = auto()
    PERMISSION_REQUIRED = auto()
    UNAVAILABLE = auto()
    UNREACHABLE = auto()


class KeybindingsDeliveryOutcome(FrozenModel):
    """One attempt's status, with what a refusal or failure said."""

    status: KeybindingsDeliveryStatus = Field(description="How the attempt ended")
    detail: str = Field(default="", description="What the refusal or failure said, for the settings screen")


class KeybindingsDeliveryStore(MutableModel):
    """Remembers the one pending permission request, so a retry never files a second."""

    state_directory: Path = Field(frozen=True, description="The shell's state directory")

    def _path(self) -> Path:
        return self.state_directory / _DELIVERY_STATE_FILENAME

    def pending_request_id(self) -> str | None:
        with STATE_FILES_LOCK:
            raw = read_json_object(self._path())
        if raw is None or raw.get("version") != _DELIVERY_STATE_VERSION:
            return None
        request_id = raw.get("pending_request_id")
        return request_id if isinstance(request_id, str) and request_id else None

    def set_pending_request_id(self, request_id: str | None) -> None:
        with STATE_FILES_LOCK:
            write_json_atomic(self._path(), {"version": _DELIVERY_STATE_VERSION, "pending_request_id": request_id})


def merged_account_keybindings(workspace_root: Path) -> tuple[dict[str, dict[str, list[str]]], dict[str, dict[str, list[str]]]]:
    """The account value to PUT (the snapshot's overlaid with the unsent saves), and the unsent part alone."""
    snapshot = load_workspace_profile_snapshot(workspace_root)
    account = parse_keybindings_value(snapshot.value_by_key.get(KEYBINDINGS_SETTING_KEY)) if snapshot is not None else {}
    unsent = parse_keybindings_value(load_unsent_change_values(workspace_root).get(KEYBINDINGS_SETTING_KEY))
    merged = {app: dict(chords_by_action) for app, chords_by_action in account.items()}
    for app_name, chords_by_action in unsent.items():
        merged.setdefault(app_name, {}).update(chords_by_action)
    return merged, unsent


def _file_permission_request(gateway: GatewayAccess, agent_id: str, store: KeybindingsDeliveryStore) -> str:
    """File the one ``workspace`` permission request for the verb; answers the detail for the screen."""
    try:
        response = httpx.post(
            gateway.url("/permission-requests"),
            json={
                "agent_id": agent_id,
                "type": "workspace",
                "payload": {"permissions": [ACCOUNT_SETTINGS_VERB], "target_workspace_id": agent_id},
                "rationale": _PERMISSION_RATIONALE,
            },
            headers=gateway.headers(),
            timeout=_DELIVERY_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as e:
        logger.warning("Could not file the keybindings permission request: {}", e)
        return "The permission request could not be filed; the change stays in this workspace for now."
    if response.status_code not in (200, 201):
        logger.warning("The gateway refused the keybindings permission request: HTTP {}", response.status_code)
        return "The permission request could not be filed; the change stays in this workspace for now."
    try:
        body = response.json()
    except json.JSONDecodeError:
        # The request was filed (the gateway answered 2xx); only its id is unreadable,
        # so it is tracked under a placeholder rather than refiled.
        logger.warning("The gateway's permission-request answer was not JSON; tracking the request without its id")
        body = None
    request_id = body["request_id"] if isinstance(body, dict) and isinstance(body.get("request_id"), str) else ""
    store.set_pending_request_id(request_id or "filed")
    return "Waiting for your approval in Imbue Studio; the change applies in this workspace meanwhile."


def deliver_unsent_keybindings(
    workspace_root: Path,
    owner_user_id: str,
    store: KeybindingsDeliveryStore,
    # Whether a 403 may file a permission request: a user's save does, an automatic retry never
    # (a denial would otherwise refile the request and nag the user forever).
    is_filing_allowed: bool,
) -> KeybindingsDeliveryOutcome:
    """One attempt at sending the unsent saves to the account through the gateway."""
    merged, unsent = merged_account_keybindings(workspace_root)
    if not unsent:
        return KeybindingsDeliveryOutcome(status=KeybindingsDeliveryStatus.NOTHING_TO_DELIVER)
    gateway = GatewayAccess.from_environ(os.environ)
    agent_id = os.environ.get(ENV_AGENT_ID, "")
    if gateway is None or not agent_id:
        return KeybindingsDeliveryOutcome(
            status=KeybindingsDeliveryStatus.UNAVAILABLE,
            detail="This workspace is not connected to Imbue Studio, so the change stays in this workspace.",
        )
    try:
        response = httpx.put(
            gateway.url(f"/minds-api-proxy/api/v1/workspaces/{agent_id}/account-settings/keybindings"),
            json={"keybindings": merged},
            headers=gateway.headers(),
            timeout=_DELIVERY_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as e:
        logger.debug("Could not deliver the keybindings saves: {}", e)
        return KeybindingsDeliveryOutcome(
            status=KeybindingsDeliveryStatus.UNREACHABLE,
            detail="Imbue Studio could not be reached; the change stays in this workspace until it can.",
        )
    if response.status_code == 200:
        # The optimistic copies clear now, but the snapshot still carries the old account value
        # until the desktop's kicked sync pass rewrites it: a /me read in that window resolves
        # the previous chord, and the snapshot watch broadcasts the correction when it lands.
        # The designed cost of never holding a request on an in-workspace write (spec section 9.2).
        clear_delivered_keybindings(workspace_root, owner_user_id, unsent)
        store.set_pending_request_id(None)
        logger.info("Delivered {} keybinding save(s) to the account", sum(len(v) for v in unsent.values()))
        return KeybindingsDeliveryOutcome(status=KeybindingsDeliveryStatus.DELIVERED)
    if response.status_code == 403:
        if store.pending_request_id() is not None:
            return KeybindingsDeliveryOutcome(
                status=KeybindingsDeliveryStatus.PERMISSION_PENDING,
                detail="Waiting for your approval in Imbue Studio; the change applies in this workspace meanwhile.",
            )
        if is_filing_allowed:
            detail = _file_permission_request(gateway, agent_id, store)
            is_filed = store.pending_request_id() is not None
            status = (
                KeybindingsDeliveryStatus.PERMISSION_PENDING if is_filed else KeybindingsDeliveryStatus.UNREACHABLE
            )
            return KeybindingsDeliveryOutcome(status=status, detail=detail)
        return KeybindingsDeliveryOutcome(
            status=KeybindingsDeliveryStatus.PERMISSION_REQUIRED,
            detail="Imbue Studio has not granted this workspace the permission; save again to re-request it.",
        )
    logger.warning("The keybindings delivery was refused: HTTP {} {}", response.status_code, response.text[:200])
    return KeybindingsDeliveryOutcome(
        status=KeybindingsDeliveryStatus.UNREACHABLE,
        detail="Imbue Studio refused the change; it stays in this workspace for now.",
    )

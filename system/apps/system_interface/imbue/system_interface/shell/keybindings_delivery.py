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

A delivered save stays in the unsent file, marked delivered, until the desktop rewrites the profile
snapshot after taking it: the rewrite rides the desktop's next sync pass, seconds later, and without
the unsent copy every read in between would resolve the previous chord. The snapshot watch settles
the marks (``settle_delivered_keybindings``) on the first snapshot written after the delivery was
sent, whatever it carries (another value there is the account moving on, from another computer). A
snapshot the desktop worked out before taking the delivery can land after it, so a snapshot only
counts once its ``written_at`` is later than the delivery; that compares the desktop's clock with
this machine's, so skew can delay a settle (harmless: the unsent copy equals the account's value) or,
beyond the seconds a pass takes, let an older snapshot settle early.
"""

import json
import os
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from enum import auto
from pathlib import Path
from typing import Any
from typing import Final

import httpx
from latchkey_access.gateway import GatewayAccess
from loguru import logger
from pydantic import Field
from pydantic import ValidationError
from workspace_profile.keybindings import KEYBINDINGS_SETTING_KEY
from workspace_profile.keybindings import parse_keybindings_value
from workspace_profile.overrides import load_unsent_change_values
from workspace_profile.snapshot import load_workspace_profile_snapshot

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.system_interface.shell.keybindings import PROFILE_FILES_LOCK
from imbue.system_interface.shell.keybindings import clear_delivered_override_copies
from imbue.system_interface.shell.keybindings import drop_settled_unsent_keybindings
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


class DeliveredKeybinding(FrozenModel):
    """One unsent save the account took, kept applying here until the profile snapshot carries it."""

    app: str = Field(description="The app the assignment is for")
    action: str = Field(description="The action")
    chords: tuple[str, ...] = Field(description="The delivered chords")
    sent_at: datetime = Field(description="When the delivery was sent; a snapshot written after it reflects it")


class KeybindingsDeliveryStore(MutableModel):
    """Remembers the one pending permission request, so a retry never files a second, and the delivered saves
    the snapshot does not carry yet."""

    state_directory: Path = Field(frozen=True, description="The shell's state directory")

    def _path(self) -> Path:
        return self.state_directory / _DELIVERY_STATE_FILENAME

    def _read(self) -> dict[str, Any]:
        raw = read_json_object(self._path())
        if raw is None or raw.get("version") != _DELIVERY_STATE_VERSION:
            return {"version": _DELIVERY_STATE_VERSION}
        return raw

    def pending_request_id(self) -> str | None:
        with STATE_FILES_LOCK:
            request_id = self._read().get("pending_request_id")
        return request_id if isinstance(request_id, str) and request_id else None

    def set_pending_request_id(self, request_id: str | None) -> None:
        with STATE_FILES_LOCK:
            document = self._read()
            document["pending_request_id"] = request_id
            write_json_atomic(self._path(), document)

    def delivered_keybindings(self) -> tuple[DeliveredKeybinding, ...]:
        with STATE_FILES_LOCK:
            raw = self._read().get("delivered")
        if not isinstance(raw, list):
            return ()
        delivered = []
        for entry in raw:
            try:
                delivered.append(DeliveredKeybinding.model_validate(entry))
            except ValidationError as e:
                logger.warning("Ignoring a malformed delivered keybinding mark: {}", e)
        return tuple(delivered)

    def _write_delivered(self, delivered: Sequence[DeliveredKeybinding]) -> None:
        document = self._read()
        document["delivered"] = [entry.model_dump(mode="json") for entry in delivered]
        write_json_atomic(self._path(), document)

    def mark_delivered(self, delivered: Mapping[str, Mapping[str, Sequence[str]]], sent_at: datetime) -> None:
        """Record the assignments the account just took, replacing an older mark of the same action."""
        with STATE_FILES_LOCK:
            kept = [
                entry for entry in self.delivered_keybindings() if entry.action not in delivered.get(entry.app, {})
            ]
            added = [
                DeliveredKeybinding(app=app_name, action=action_id, chords=tuple(chords), sent_at=sent_at)
                for app_name, chords_by_action in delivered.items()
                for action_id, chords in chords_by_action.items()
            ]
            self._write_delivered([*kept, *added])

    def forget_delivered(self, app_name: str, action_id: str) -> None:
        """Drop an action's mark: a new save of it is undelivered whatever it assigns."""
        with STATE_FILES_LOCK:
            kept = [
                entry
                for entry in self.delivered_keybindings()
                if not (entry.app == app_name and entry.action == action_id)
            ]
            self._write_delivered(kept)

    def remove_delivered(self, settled: Sequence[DeliveredKeybinding]) -> None:
        with STATE_FILES_LOCK:
            self._write_delivered([entry for entry in self.delivered_keybindings() if entry not in settled])


def delivered_chords_by_app(delivered: Sequence[DeliveredKeybinding]) -> dict[str, dict[str, list[str]]]:
    """The delivered marks as one keybindings map."""
    by_app: dict[str, dict[str, list[str]]] = {}
    for entry in delivered:
        by_app.setdefault(entry.app, {})[entry.action] = list(entry.chords)
    return by_app


@pure
def undelivered_keybindings(
    unsent: Mapping[str, Mapping[str, Sequence[str]]],
    delivered: Mapping[str, Mapping[str, Sequence[str]]],
) -> dict[str, dict[str, list[str]]]:
    """The unsent assignments the account has not taken: those with no delivered mark of the same chords."""
    undelivered: dict[str, dict[str, list[str]]] = {}
    for app_name, chords_by_action in unsent.items():
        for action_id, chords in chords_by_action.items():
            if list(delivered.get(app_name, {}).get(action_id, ())) != list(chords):
                undelivered.setdefault(app_name, {})[action_id] = list(chords)
    return undelivered


def merged_account_keybindings(
    workspace_root: Path,
) -> tuple[dict[str, dict[str, list[str]]], dict[str, dict[str, list[str]]]]:
    """The account value to PUT (the snapshot's overlaid with the unsent saves), and the unsent part alone."""
    snapshot = load_workspace_profile_snapshot(workspace_root)
    account = (
        parse_keybindings_value(snapshot.value_by_key.get(KEYBINDINGS_SETTING_KEY)) if snapshot is not None else {}
    )
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
    undelivered = undelivered_keybindings(unsent, delivered_chords_by_app(store.delivered_keybindings()))
    if not undelivered:
        return KeybindingsDeliveryOutcome(status=KeybindingsDeliveryStatus.NOTHING_TO_DELIVER)
    gateway = GatewayAccess.from_environ(os.environ)
    agent_id = os.environ.get(ENV_AGENT_ID, "")
    if gateway is None or not agent_id:
        return KeybindingsDeliveryOutcome(
            status=KeybindingsDeliveryStatus.UNAVAILABLE,
            detail="This workspace is not connected to Imbue Studio, so the change stays in this workspace.",
        )
    sent_at = datetime.now(timezone.utc)
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
        # One step against a save of the same action: a save landing between the two would keep its override copy
        # and find its value already marked, so it would never be sent or cleared.
        with PROFILE_FILES_LOCK:
            clear_delivered_override_copies(workspace_root, owner_user_id, undelivered)
            store.mark_delivered(undelivered, sent_at)
            # The rewrite may already have landed, its watch having fired before the mark existed.
            settle_delivered_keybindings(workspace_root, store)
        store.set_pending_request_id(None)
        logger.info("Delivered {} keybinding save(s) to the account", sum(len(v) for v in undelivered.values()))
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


def _written_after(snapshot_written_at: str, sent_at: datetime) -> bool:
    """Whether the snapshot was written after the delivery was sent; an unreadable time counts as after, so a mark
    never outlives the snapshot that should settle it."""
    try:
        written_at = datetime.fromisoformat(snapshot_written_at)
    except ValueError:
        return True
    if written_at.tzinfo is None:
        written_at = written_at.replace(tzinfo=timezone.utc)
    return written_at > sent_at


def settle_delivered_keybindings(workspace_root: Path, store: KeybindingsDeliveryStore) -> None:
    """Drop the delivered saves a snapshot written after them answers for now, with their unsent copies."""
    with PROFILE_FILES_LOCK:
        delivered = store.delivered_keybindings()
        if not delivered:
            return
        snapshot = load_workspace_profile_snapshot(workspace_root)
        if snapshot is None:
            return
        settled = [entry for entry in delivered if _written_after(snapshot.written_at, entry.sent_at)]
        if not settled:
            return
        drop_settled_unsent_keybindings(workspace_root, delivered_chords_by_app(settled))
        store.remove_delivered(settled)
    logger.info("The profile snapshot now answers {} delivered keybinding save(s)", len(settled))

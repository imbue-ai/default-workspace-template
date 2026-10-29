"""The shell's keybindings routes: its own manifest and resolved chords, the whole-workspace catalog
the Keyboard settings lists, and the save, discard, and deliver flows (user-profile-state spec,
sections 9.3 and 10.3)."""

import os
from enum import auto
from typing import Final

from app_schema.routes import model_response
from app_schema.routes import parse_request_body
from flask import Flask
from flask.typing import ResponseReturnValue
from pydantic import Field
from workspace_profile.errors import InvalidChordError
from workspace_profile.keybindings import normalize_chord
from workspace_profile.keybindings import resolve_app_keybindings
from workspace_profile.manifest import resolve_keybindings_me_response
from workspace_profile.resolve import resolve_request_user_id
from workspace_profile.snapshot import load_workspace_profile_snapshot

from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.system_interface.app_context import get_state
from imbue.system_interface.shell.gateway_access import GatewayAccess
from imbue.system_interface.shell.keybindings import SHELL_KEYBINDINGS_MANIFEST
from imbue.system_interface.shell.keybindings import discard_unsent_keybinding
from imbue.system_interface.shell.keybindings import fetch_app_keybindings_manifests
from imbue.system_interface.shell.keybindings import remove_user_keybinding
from imbue.system_interface.shell.keybindings import resolve_assignment_source
from imbue.system_interface.shell.keybindings import save_unsent_keybinding
from imbue.system_interface.shell.keybindings import save_user_keybinding
from imbue.system_interface.shell.keybindings_delivery import ENV_AGENT_ID
from imbue.system_interface.shell.keybindings_delivery import KeybindingsDeliveryOutcome
from imbue.system_interface.shell.keybindings_delivery import KeybindingsDeliveryStatus
from imbue.system_interface.shell.keybindings_delivery import KeybindingsDeliveryStore
from imbue.system_interface.shell.keybindings_delivery import deliver_unsent_keybindings
from imbue.system_interface.shell.keybindings_delivery import merged_account_keybindings
from imbue.system_interface.shell.route_helpers import HTTP_BAD_REQUEST
from imbue.system_interface.shell.route_helpers import HTTP_FORBIDDEN
from imbue.system_interface.shell.route_helpers import detail_response
from imbue.system_interface.shell.route_helpers import request_identity
from imbue.system_interface.shell.state import ShellState

KEYBINDINGS_ACTIONS_ROUTE: Final[str] = "/api/keybindings/actions"
KEYBINDINGS_ME_ROUTE: Final[str] = "/api/keybindings/me"
KEYBINDINGS_CATALOG_ROUTE: Final[str] = "/api/keybindings/catalog"
KEYBINDINGS_SAVE_ROUTE: Final[str] = "/api/keybindings"
KEYBINDINGS_DISCARD_ROUTE: Final[str] = "/api/keybindings/discard"
KEYBINDINGS_RESET_ROUTE: Final[str] = "/api/keybindings/reset"
KEYBINDINGS_DELIVER_ROUTE: Final[str] = "/api/keybindings/deliver"


class KeybindingSaveScope(LowerCaseStrEnum):
    """Where a save applies (a wire value): this workspace alone, or the owner's whole account."""

    WORKSPACE = auto()
    ACCOUNT = auto()


class PermissionResolutionVerdict(LowerCaseStrEnum):
    """How the user answered a permission request (the chrome's wire value)."""

    GRANTED = auto()
    DENIED = auto()


class KeybindingActionView(FrozenModel):
    """One action as the Keyboard settings shows it: the declaration, resolved for the requester."""

    id: str = Field(description="The action id")
    label: str = Field(description="What the action does")
    default_chords: tuple[str, ...] = Field(description="The app's out-of-the-box chords")
    chords: tuple[str, ...] = Field(description="The chords resolved for this requester")
    source: str = Field(description="Which layer answered: default, account, unsent, or workspace")


class KeybindingAppView(FrozenModel):
    """One app's actions in the catalog."""

    app: str = Field(description="The app's name, the key its assignments are stored under")
    display_name: str = Field(description="The app's name as users see it")
    actions: tuple[KeybindingActionView, ...] = Field(description="The declared actions, resolved")


class UnsentKeybindingView(FrozenModel):
    """One "everywhere" save not yet delivered to the account."""

    app: str = Field(description="The app the assignment is for")
    action: str = Field(description="The action")
    chords: tuple[str, ...] = Field(description="The saved chords")


class KeybindingsCatalogResponse(FrozenModel):
    """Everything the Keyboard settings renders."""

    apps: tuple[KeybindingAppView, ...] = Field(description="Every app's actions, resolved for the requester")
    is_owner: bool = Field(description="Whether the requester is the workspace's owner")
    can_save_everywhere: bool = Field(
        description="Whether an 'everywhere' save can reach the account (a gateway and a workspace id exist)"
    )
    has_pending_permission: bool = Field(description="Whether a filed permission request awaits the user's answer")
    unsent: tuple[UnsentKeybindingView, ...] = Field(description="The undelivered 'everywhere' saves")


class SaveKeybindingRequest(FrozenModel):
    """One assignment to save: where it applies is the scope."""

    app: str = Field(description="The app the action belongs to")
    action: str = Field(description="The action to re-bind")
    chords: tuple[str, ...] = Field(description="The chords to assign, in any spelling the recorder makes")
    scope: KeybindingSaveScope = Field(
        description="'workspace' saves only here (the requester's override); 'account' saves everywhere (owner only)"
    )


class SaveKeybindingResponse(FrozenModel):
    """What a save did."""

    chords: tuple[str, ...] = Field(description="The assignment as stored, in the canonical spelling")
    delivery: KeybindingsDeliveryOutcome | None = Field(
        description="How the account delivery went, for an 'account' save; null for a workspace one"
    )


class DiscardKeybindingRequest(FrozenModel):
    """One undelivered 'everywhere' save to drop."""

    app: str = Field(description="The app the assignment is for")
    action: str = Field(description="The action")


class ResetKeybindingRequest(FrozenModel):
    """One workspace override of the requester's to drop, so the account value (or default) answers again."""

    app: str = Field(description="The app the assignment is for")
    action: str = Field(description="The action")


class PermissionResolutionEntry(FrozenModel):
    """One verdict out of the chrome's ``minds:permission-resolutions`` snapshot."""

    request_id: str = Field(description="The permission request the verdict answers")
    resolution: PermissionResolutionVerdict = Field(description="How the user answered it")


class DeliverKeybindingsRequest(FrozenModel):
    """A delivery retry, optionally carrying the permission verdicts the page observed."""

    resolution: PermissionResolutionVerdict | None = Field(
        default=None, description="A verdict known to answer this workspace's own request, when the caller knows it"
    )
    resolutions: tuple[PermissionResolutionEntry, ...] = Field(
        default=(),
        description="The chrome's verdict snapshot; only the entry naming the filed request counts",
    )


def _shell() -> ShellState:
    return get_state().shell


def _can_save_everywhere() -> bool:
    return GatewayAccess.from_environ(os.environ) is not None and bool(os.environ.get(ENV_AGENT_ID, ""))


def get_keybindings_actions() -> ResponseReturnValue:
    """The shell's own keybindings manifest."""
    return model_response(SHELL_KEYBINDINGS_MANIFEST)


def get_keybindings_me() -> ResponseReturnValue:
    """The requesting user's resolved chords for the shell's own actions."""
    shell = _shell()
    return model_response(
        resolve_keybindings_me_response(
            SHELL_KEYBINDINGS_MANIFEST,
            snapshot=load_workspace_profile_snapshot(shell.repo_root),
            identity=request_identity(),
            workspace_root=shell.repo_root,
        )
    )


def get_keybindings_catalog() -> ResponseReturnValue:
    """Every app's actions resolved for the requester, with the save surfaces' state."""
    shell = _shell()
    identity = request_identity()
    snapshot = load_workspace_profile_snapshot(shell.repo_root)
    apps = []
    for app_manifest in fetch_app_keybindings_manifests(shell.inventory.entries()):
        manifest = app_manifest.manifest
        resolved = resolve_app_keybindings(
            manifest.app,
            {action.id: action.default_chords for action in manifest.actions},
            snapshot=snapshot,
            identity=identity,
            workspace_root=shell.repo_root,
        )
        actions = tuple(
            KeybindingActionView(
                id=action.id,
                label=action.label,
                default_chords=action.default_chords,
                chords=tuple(resolved[action.id]),
                source=resolve_assignment_source(
                    manifest.app, action.id, snapshot=snapshot, identity=identity, workspace_root=shell.repo_root
                ),
            )
            for action in manifest.actions
        )
        apps.append(KeybindingAppView(app=manifest.app, display_name=app_manifest.display_name, actions=actions))
    _merged, unsent = merged_account_keybindings(shell.repo_root)
    unsent_views = tuple(
        UnsentKeybindingView(app=app_name, action=action_id, chords=tuple(chords))
        for app_name, chords_by_action in sorted(unsent.items())
        for action_id, chords in sorted(chords_by_action.items())
    )
    store = KeybindingsDeliveryStore(state_directory=shell.state_directory)
    return model_response(
        KeybindingsCatalogResponse(
            apps=tuple(apps),
            is_owner=identity.owner,
            can_save_everywhere=_can_save_everywhere(),
            has_pending_permission=store.pending_request_id() is not None,
            unsent=unsent_views if identity.owner else (),
        )
    )


def save_keybinding() -> ResponseReturnValue:
    """Save one assignment: the requester's override here, or (the owner's 'account' scope) everywhere."""
    body = parse_request_body(SaveKeybindingRequest)
    shell = _shell()
    identity = request_identity()
    snapshot = load_workspace_profile_snapshot(shell.repo_root)
    try:
        chords = [normalize_chord(chord) for chord in body.chords]
    except InvalidChordError as e:
        return detail_response(str(e), HTTP_BAD_REQUEST)
    is_known_action = any(
        app_manifest.manifest.app == body.app and any(action.id == body.action for action in app_manifest.manifest.actions)
        for app_manifest in fetch_app_keybindings_manifests(shell.inventory.entries())
    )
    if not is_known_action:
        return detail_response(f"no app declares the action {body.app}.{body.action}", HTTP_BAD_REQUEST)
    user_id = resolve_request_user_id(identity, snapshot)
    if user_id is None:
        return detail_response("a visitor with no account cannot save keybindings", HTTP_FORBIDDEN)
    if body.scope is KeybindingSaveScope.ACCOUNT:
        if not identity.owner:
            return detail_response("only the owner can save a keybinding for every workspace", HTTP_FORBIDDEN)
        save_unsent_keybinding(shell.repo_root, user_id, body.app, body.action, chords)
        shell.broadcast_keybindings_changed()
        store = KeybindingsDeliveryStore(state_directory=shell.state_directory)
        delivery = deliver_unsent_keybindings(shell.repo_root, user_id, store, is_filing_allowed=True)
        return model_response(SaveKeybindingResponse(chords=tuple(chords), delivery=delivery))
    save_user_keybinding(shell.repo_root, user_id, body.app, body.action, chords)
    shell.broadcast_keybindings_changed()
    return model_response(SaveKeybindingResponse(chords=tuple(chords), delivery=None))


def discard_keybinding() -> ResponseReturnValue:
    """Drop one undelivered 'everywhere' save (and its optimistic copy here)."""
    body = parse_request_body(DiscardKeybindingRequest)
    shell = _shell()
    identity = request_identity()
    if not identity.owner:
        return detail_response("only the owner discards 'everywhere' saves", HTTP_FORBIDDEN)
    snapshot = load_workspace_profile_snapshot(shell.repo_root)
    owner_user_id = resolve_request_user_id(identity, snapshot) or "local"
    discard_unsent_keybinding(shell.repo_root, owner_user_id, body.app, body.action)
    shell.broadcast_keybindings_changed()
    return model_response(KeybindingsDeliveryOutcome(status=KeybindingsDeliveryStatus.NOTHING_TO_DELIVER))


def reset_keybinding() -> ResponseReturnValue:
    """Drop the requester's workspace override of one action."""
    body = parse_request_body(ResetKeybindingRequest)
    shell = _shell()
    identity = request_identity()
    snapshot = load_workspace_profile_snapshot(shell.repo_root)
    user_id = resolve_request_user_id(identity, snapshot)
    if user_id is None:
        return detail_response("a visitor with no account holds no keybindings here", HTTP_FORBIDDEN)
    remove_user_keybinding(shell.repo_root, user_id, body.app, body.action)
    shell.broadcast_keybindings_changed()
    return model_response(KeybindingsDeliveryOutcome(status=KeybindingsDeliveryStatus.NOTHING_TO_DELIVER))


def deliver_keybindings() -> ResponseReturnValue:
    """Retry the undelivered 'everywhere' saves, after a permission verdict or on the user's ask."""
    body = parse_request_body(DeliverKeybindingsRequest)
    shell = _shell()
    identity = request_identity()
    if not identity.owner:
        return detail_response("only the owner delivers 'everywhere' saves", HTTP_FORBIDDEN)
    store = KeybindingsDeliveryStore(state_directory=shell.state_directory)
    pending_request_id = store.pending_request_id()
    matched = next((entry for entry in body.resolutions if entry.request_id == pending_request_id), None)
    resolution = body.resolution if body.resolution is not None else (matched.resolution if matched else None)
    if resolution is not None:
        # The verdict answers the filed request either way; a denial leaves the saves
        # here (retried or discarded from the Keyboard settings) without re-asking.
        store.set_pending_request_id(None)
    if resolution is PermissionResolutionVerdict.DENIED:
        return model_response(
            KeybindingsDeliveryOutcome(
                status=KeybindingsDeliveryStatus.PERMISSION_REQUIRED,
                detail="The request was declined; the change stays in this workspace until delivered or discarded.",
            )
        )
    snapshot = load_workspace_profile_snapshot(shell.repo_root)
    owner_user_id = resolve_request_user_id(identity, snapshot) or "local"
    return model_response(deliver_unsent_keybindings(shell.repo_root, owner_user_id, store, is_filing_allowed=False))


def register_keybindings_routes(application: Flask) -> None:
    """Wire the keybindings routes onto the Flask application."""
    application.add_url_rule(
        KEYBINDINGS_ACTIONS_ROUTE, view_func=get_keybindings_actions, methods=["GET"], endpoint="keybindings_actions"
    )
    application.add_url_rule(
        KEYBINDINGS_ME_ROUTE, view_func=get_keybindings_me, methods=["GET"], endpoint="keybindings_me"
    )
    application.add_url_rule(
        KEYBINDINGS_CATALOG_ROUTE, view_func=get_keybindings_catalog, methods=["GET"], endpoint="keybindings_catalog"
    )
    application.add_url_rule(
        KEYBINDINGS_SAVE_ROUTE, view_func=save_keybinding, methods=["POST"], endpoint="save_keybinding"
    )
    application.add_url_rule(
        KEYBINDINGS_DISCARD_ROUTE, view_func=discard_keybinding, methods=["POST"], endpoint="discard_keybinding"
    )
    application.add_url_rule(
        KEYBINDINGS_RESET_ROUTE, view_func=reset_keybinding, methods=["POST"], endpoint="reset_keybinding"
    )
    application.add_url_rule(
        KEYBINDINGS_DELIVER_ROUTE, view_func=deliver_keybindings, methods=["POST"], endpoint="deliver_keybindings"
    )

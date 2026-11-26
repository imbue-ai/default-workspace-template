"""The workspace routes (``/api/workspace/...``): the state a workspace owns, read and changed over HTTP by a
client outside it, where a client used to run a command in the workspace.

This module holds only what turns a request into a call and an answer into a response. What each route does lives
in a library: the grants in ``share_grants``, the profile snapshot and the timezone in ``workspace_profile``, the
backup configuration in ``workspace_backup_setup``, and the updates in ``workspace_update_control``.

A route asks for no more than the request guard already does: it reads the requester's identity, so a header that
does not parse is refused, and it does not ask for the owner, since whoever a proxy admits to this internal app
already reaches the terminal, and with it every file a route writes. The one exception is the read of the backup
credentials, which answers a secret to a client outside the workspace, and so answers the owner alone.

A refusal is ``{"detail", "code"}`` under a 4xx, a route that could not do what it was asked is the same under a
5xx, and an answer that describes what the workspace did, including that it declined, is a 200 with an ``outcome``.
"""

import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final
from typing import TypeVar

from app_schema.api_schema import AlternateResponse
from app_schema.api_schema import RouteModels
from app_schema.api_schema import RouteTable
from app_schema.errors import InvalidRequestBodyError
from app_schema.routes import model_response
from app_schema.routes import parse_request_body
from flask import Flask
from flask import request
from flask.typing import ResponseReturnValue
from loguru import logger
from pydantic import BaseModel
from share_grants.document import GrantEntries
from share_grants.document import GrantedIdentityEntry
from share_grants.document import grant_entries
from share_grants.document import grant_lists_by_app
from share_grants.document import grant_shape_refusals
from share_grants.document import grants_entries
from share_grants.document import normalized_grants
from share_grants.document import read_sharing_state
from share_grants.document import replace_grants
from share_grants.grants import GrantList
from share_grants.grants import Grants
from workspace_backup_setup.destination import BackupDestinationRequest
from workspace_backup_setup.destination import request_backup_destination
from workspace_backup_setup.errors import BackupOperationRunningError
from workspace_backup_setup.errors import InvalidBackupDestinationError
from workspace_backup_setup.state import BackupCredentials
from workspace_backup_setup.state import BackupLeaseHolder
from workspace_backup_setup.state import BackupState
from workspace_backup_setup.state import read_backup_credentials
from workspace_backup_setup.state import read_backup_state
from workspace_backup_setup.state import workspace_id_from_environment
from workspace_backup_setup.verification import read_verification_disabled
from workspace_backup_setup.verification import write_verification_disabled
from workspace_profile.errors import InvalidProfileSnapshotError
from workspace_profile.errors import InvalidTimezoneError
from workspace_profile.errors import ProfileWriteError
from workspace_profile.snapshot import write_workspace_profile_snapshot
from workspace_profile.timezone import read_user_timezone
from workspace_profile.timezone import write_user_timezone
from workspace_update_control.agents import read_observed_agents
from workspace_update_control.control import AgentsReader
from workspace_update_control.control import arm_schedule
from workspace_update_control.control import cancel_schedule
from workspace_update_control.control import read_liveness
from workspace_update_control.control import read_update_state
from workspace_update_control.control import record_dismissals
from workspace_update_control.control import refresh_schedule
from workspace_update_control.control import skip_target
from workspace_update_control.control import start_update
from workspace_update_control.dispatcher import AgentEnvironment
from workspace_update_control.dispatcher import PROC_ROOT
from workspace_update_control.protocol import OUTCOME_STARTING
from workspace_update_control.settings import InvalidUpdateSettingsError
from workspace_update_control.settings import UpdateSettings
from workspace_update_control.settings import write_update_settings

from imbue.system_interface.app_context import get_state
from imbue.system_interface.shell.route_helpers import HTTP_ACCEPTED
from imbue.system_interface.shell.route_helpers import HTTP_BAD_REQUEST
from imbue.system_interface.shell.route_helpers import HTTP_CONFLICT
from imbue.system_interface.shell.route_helpers import HTTP_FORBIDDEN
from imbue.system_interface.shell.route_helpers import HTTP_INTERNAL_ERROR
from imbue.system_interface.shell.route_helpers import HTTP_OK
from imbue.system_interface.shell.route_helpers import request_identity
from imbue.system_interface.template_ref import read_template_ref
from imbue.system_interface.workspace_api_models import BackupOperationRunningRefusal
from imbue.system_interface.workspace_api_models import BackupVerificationModel
from imbue.system_interface.workspace_api_models import CODE_BACKUP_OPERATION_RUNNING
from imbue.system_interface.workspace_api_models import CODE_GRANTS_UNREADABLE
from imbue.system_interface.workspace_api_models import CODE_INVALID_DESTINATION
from imbue.system_interface.workspace_api_models import CODE_INVALID_GRANTS
from imbue.system_interface.workspace_api_models import CODE_INVALID_PROFILE
from imbue.system_interface.workspace_api_models import CODE_INVALID_REQUEST
from imbue.system_interface.workspace_api_models import CODE_INVALID_TIMEZONE
from imbue.system_interface.workspace_api_models import CODE_INVALID_UPDATE_SETTINGS
from imbue.system_interface.workspace_api_models import CODE_NOT_AT_WORKSPACE_ROOT
from imbue.system_interface.workspace_api_models import CODE_OWNER_ONLY
from imbue.system_interface.workspace_api_models import CODE_PREVIEW
from imbue.system_interface.workspace_api_models import CODE_WRITE_FAILED
from imbue.system_interface.workspace_api_models import GRANTS_ERROR_UNREADABLE
from imbue.system_interface.workspace_api_models import GRANTS_PER_APP_KEY
from imbue.system_interface.workspace_api_models import GrantListModel
from imbue.system_interface.workspace_api_models import GrantRefusalEntry
from imbue.system_interface.workspace_api_models import GrantsDocumentModel
from imbue.system_interface.workspace_api_models import GrantsStoredResponse
from imbue.system_interface.workspace_api_models import InvalidGrantsError
from imbue.system_interface.workspace_api_models import OPERATION_BACKUPS_CREDENTIALS_READ
from imbue.system_interface.workspace_api_models import OPERATION_BACKUPS_DESTINATION_WRITE
from imbue.system_interface.workspace_api_models import OPERATION_BACKUPS_READ
from imbue.system_interface.workspace_api_models import OPERATION_BACKUPS_VERIFICATION_WRITE
from imbue.system_interface.workspace_api_models import OPERATION_PROFILE_WRITE
from imbue.system_interface.workspace_api_models import OPERATION_SHARING_GRANTS_WRITE
from imbue.system_interface.workspace_api_models import OPERATION_SHARING_READ
from imbue.system_interface.workspace_api_models import OPERATION_TIMEZONE_WRITE
from imbue.system_interface.workspace_api_models import OPERATION_UPDATE_DISMISS
from imbue.system_interface.workspace_api_models import OPERATION_UPDATE_LIVENESS_READ
from imbue.system_interface.workspace_api_models import OPERATION_UPDATE_READ
from imbue.system_interface.workspace_api_models import OPERATION_UPDATE_SCHEDULE_CANCEL
from imbue.system_interface.workspace_api_models import OPERATION_UPDATE_SCHEDULE_REFRESH
from imbue.system_interface.workspace_api_models import OPERATION_UPDATE_SCHEDULE_WRITE
from imbue.system_interface.workspace_api_models import OPERATION_UPDATE_SETTINGS_WRITE
from imbue.system_interface.workspace_api_models import OPERATION_UPDATE_SKIP
from imbue.system_interface.workspace_api_models import OPERATION_UPDATE_START
from imbue.system_interface.workspace_api_models import ProfileSnapshotRequest
from imbue.system_interface.workspace_api_models import ProfileWrittenResponse
from imbue.system_interface.workspace_api_models import ReplaceGrantsRequest
from imbue.system_interface.workspace_api_models import ShareableApp
from imbue.system_interface.workspace_api_models import SharingStateResponse
from imbue.system_interface.workspace_api_models import TimezoneRequest
from imbue.system_interface.workspace_api_models import TimezoneResponse
from imbue.system_interface.workspace_api_models import UpdateBusyChat
from imbue.system_interface.workspace_api_models import UpdateChannelVersion
from imbue.system_interface.workspace_api_models import UpdateDismissalsRequest
from imbue.system_interface.workspace_api_models import UpdateDismissalsResponse
from imbue.system_interface.workspace_api_models import UpdateLivenessAgent
from imbue.system_interface.workspace_api_models import UpdateLivenessResponse
from imbue.system_interface.workspace_api_models import UpdateOutcomeResponse
from imbue.system_interface.workspace_api_models import UpdateScheduleCancelledResponse
from imbue.system_interface.workspace_api_models import UpdateScheduleRefreshRequest
from imbue.system_interface.workspace_api_models import UpdateScheduleRefreshedResponse
from imbue.system_interface.workspace_api_models import UpdateScheduleRequest
from imbue.system_interface.workspace_api_models import UpdateSettingsModel
from imbue.system_interface.workspace_api_models import UpdateSkipRequest
from imbue.system_interface.workspace_api_models import UpdateSkippedResponse
from imbue.system_interface.workspace_api_models import UpdateStartAttempt
from imbue.system_interface.workspace_api_models import UpdateStartRequest
from imbue.system_interface.workspace_api_models import UpdateStateResponse
from imbue.system_interface.workspace_api_models import WORKSPACE_COMPATIBILITY
from imbue.system_interface.workspace_api_models import WORKSPACE_OPERATIONS
from imbue.system_interface.workspace_api_models import WORKSPACE_STATUS_SCHEMA
from imbue.system_interface.workspace_api_models import WorkspaceRouteError
from imbue.system_interface.workspace_api_models import WorkspaceStatusResponse

WORKSPACE_ROUTE_PREFIX: Final[str] = "/api/workspace"
STATUS_ROUTE: Final[str] = f"{WORKSPACE_ROUTE_PREFIX}/status"
SHARING_ROUTE: Final[str] = f"{WORKSPACE_ROUTE_PREFIX}/sharing"
SHARING_GRANTS_ROUTE: Final[str] = f"{SHARING_ROUTE}/grants"
PROFILE_ROUTE: Final[str] = f"{WORKSPACE_ROUTE_PREFIX}/profile"
TIMEZONE_ROUTE: Final[str] = f"{WORKSPACE_ROUTE_PREFIX}/timezone"
BACKUPS_ROUTE: Final[str] = f"{WORKSPACE_ROUTE_PREFIX}/backups"
BACKUP_DESTINATION_ROUTE: Final[str] = f"{BACKUPS_ROUTE}/destination"
BACKUP_CREDENTIALS_ROUTE: Final[str] = f"{BACKUPS_ROUTE}/credentials"
BACKUP_VERIFICATION_ROUTE: Final[str] = f"{BACKUPS_ROUTE}/verification"
UPDATE_ROUTE: Final[str] = f"{WORKSPACE_ROUTE_PREFIX}/update"
UPDATE_START_ROUTE: Final[str] = f"{UPDATE_ROUTE}/start"
UPDATE_SCHEDULE_ROUTE: Final[str] = f"{UPDATE_ROUTE}/schedule"
UPDATE_SKIP_ROUTE: Final[str] = f"{UPDATE_ROUTE}/skip"
UPDATE_DISMISSALS_ROUTE: Final[str] = f"{UPDATE_ROUTE}/dismissals"
UPDATE_LIVENESS_ROUTE: Final[str] = f"{UPDATE_ROUTE}/liveness"
UPDATE_SETTINGS_ROUTE: Final[str] = f"{UPDATE_ROUTE}/settings"

# The query parameter that asks the update read whether an account is signed in.
CHECK_SIGN_IN_PARAMETER: Final[str] = "check_sign_in"

_PREVIEW_REFUSAL_DETAIL: Final[str] = "This is a preview of a proposed change; it cannot change the live workspace."
_OWNER_ONLY_REFUSAL_DETAIL: Final[str] = "Only the workspace's owner may read its backup credentials."

_RequestModel = TypeVar("_RequestModel", bound=BaseModel)

# Every status a workspace route may answer its error object under.
_ERROR_STATUSES: Final[tuple[int, ...]] = (HTTP_BAD_REQUEST, HTTP_FORBIDDEN, HTTP_CONFLICT, HTTP_INTERNAL_ERROR)


class WorkspaceRouteRefusal(Exception):
    """A workspace route's refusal or failure, answered as its error object under ``status``."""

    def __init__(self, detail: str, code: str, status: int) -> None:
        super().__init__(detail)
        self.detail = detail
        self.code = code
        self.status = status


def _answer_refusal(refusal: WorkspaceRouteRefusal) -> ResponseReturnValue:
    return model_response(WorkspaceRouteError(detail=refusal.detail, code=refusal.code), refusal.status)


def _workspace_root() -> Path:
    return get_state().shell.repo_root


def _admit(is_changing_the_workspace: bool) -> None:
    """What every workspace route does first: read the requester's identity (a header that does not parse is a 400),
    and refuse, in a preview shell, a route that would change the live workspace."""
    request_identity()
    if is_changing_the_workspace and get_state().is_preview:
        raise WorkspaceRouteRefusal(_PREVIEW_REFUSAL_DETAIL, CODE_PREVIEW, HTTP_FORBIDDEN)


def _body(model: type[_RequestModel]) -> _RequestModel:
    """The request's body as ``model``; a body of another shape is refused with the routes' own error object."""
    try:
        return parse_request_body(model)
    except InvalidRequestBodyError as e:
        raise WorkspaceRouteRefusal(str(e), CODE_INVALID_REQUEST, HTTP_BAD_REQUEST) from e


def _run_in_background(work: Callable[[], None]) -> None:
    threading.Thread(target=work, daemon=True, name="workspace-route-work").start()


# The status


def workspace_status() -> ResponseReturnValue:
    """What this workspace serves: the version of its checkout, the name of every operation it has a route for,
    and the template's compatibility number.

    A client asks before the first operation it sends a workspace, and calls a route only for an operation named
    here. A preview shell names none: it serves a copy, and cannot change the live workspace. A client that reads
    a compatibility number higher than it knows opens the workspace and does nothing else inside it.
    """
    _admit(is_changing_the_workspace=False)
    return model_response(
        WorkspaceStatusResponse(
            schema=WORKSPACE_STATUS_SCHEMA,
            template_ref=read_template_ref(_workspace_root()),
            operations=() if get_state().is_preview else WORKSPACE_OPERATIONS,
            compatibility=WORKSPACE_COMPATIBILITY,
        )
    )


# Sharing


def _grant_list_model(grant_list: GrantList) -> GrantListModel:
    return GrantListModel(
        users=tuple(sorted(grant_list.users)),
        emails=tuple(sorted(grant_list.emails)),
        email_domains=tuple(sorted(grant_list.email_domains)),
    )


def _grants_document_model(grants: Grants) -> GrantsDocumentModel:
    return GrantsDocumentModel.model_validate(
        {
            "workspace": _grant_list_model(grants.workspace),
            GRANTS_PER_APP_KEY: {
                name: _grant_list_model(grant_list) for name, grant_list in grant_lists_by_app(grants).items()
            },
        }
    )


def _grant_entries(grant_list: GrantListModel) -> GrantEntries:
    return grant_entries(grant_list.users, grant_list.emails, grant_list.email_domains)


def read_sharing() -> ResponseReturnValue:
    """Whether the workspace holds share materials, its grants, and the apps that can be shared by themselves:
    what a client reads before every edit of who the workspace is shared with.

    The apps are the registered ones that are not internal, each with its name, its origin label and the name a
    person reads for it.
    """
    _admit(is_changing_the_workspace=False)
    state = read_sharing_state(_workspace_root())
    rows = [entry.row for entry in get_state().shell.inventory.entries()]
    return model_response(
        SharingStateResponse(
            is_shared=state.is_shared,
            grants=_grants_document_model(state.grants) if state.grants is not None else None,
            grants_error=GRANTS_ERROR_UNREADABLE if state.is_grants_unreadable else "",
            shareable_apps=tuple(
                ShareableApp(
                    name=str(row.name),
                    label=row.label,
                    display_name=str(row.display_name or ""),
                    is_system=row.system,
                )
                for row in rows
                if not row.internal
            ),
        )
    )


def replace_sharing_grants() -> ResponseReturnValue:
    """Replace the grants, and the identities beside them, and answer the document as stored.

    The document's shape is checked (an entry is empty, repeated, or not an email address or a domain), never who
    an account may share with, which is the caller's to decide. The value of an entry the stored document already
    holds is not checked. A document refused changes no file.
    """
    _admit(is_changing_the_workspace=True)
    body = _body(ReplaceGrantsRequest)
    workspace_root = _workspace_root()
    state = read_sharing_state(workspace_root)
    if state.is_grants_unreadable:
        raise WorkspaceRouteRefusal(
            "The grants file is there and cannot be read, so it was not replaced.",
            CODE_GRANTS_UNREADABLE,
            HTTP_INTERNAL_ERROR,
        )
    entries = grants_entries(
        _grant_entries(body.grants.workspace),
        {name: _grant_entries(grant_list) for name, grant_list in body.grants.apps.items()},
    )
    refusals = grant_shape_refusals(entries, state.grants)
    if refusals:
        return model_response(
            InvalidGrantsError(
                detail="The grants document carries entries that cannot be granted.",
                code=CODE_INVALID_GRANTS,
                refusals=tuple(
                    GrantRefusalEntry(
                        scope=refusal.scope, kind=refusal.kind, value=refusal.value, message=refusal.message
                    )
                    for refusal in refusals
                ),
            ),
            HTTP_BAD_REQUEST,
        )
    grants = normalized_grants(entries)
    identities = {
        user_id: GrantedIdentityEntry(email=identity.email, display_name=identity.display_name)
        for user_id, identity in body.identities.items()
    }
    try:
        replace_grants(workspace_root, grants, identities)
    except OSError as e:
        logger.opt(exception=e).error("Could not write the workspace's grants")
        raise WorkspaceRouteRefusal(
            f"The grants could not be written: {e}", CODE_WRITE_FAILED, HTTP_INTERNAL_ERROR
        ) from e
    logger.info("Replaced the workspace's grants ({} app scopes)", len(body.grants.apps))
    return model_response(GrantsStoredResponse(grants=_grants_document_model(grants)))


# The profile snapshot and the timezone


def write_profile() -> ResponseReturnValue:
    """Write the profile snapshot a client materialized from the workspace's account, as it is.

    A snapshot of a format newer than this template knows is written all the same. One that equals the stored
    snapshot apart from when it was written leaves the file alone and answers ``is_written`` false, so a client
    that sends the snapshot whenever it opens the workspace announces no change that did not happen. The shell
    tells its pages when the keybindings in it change.
    """
    _admit(is_changing_the_workspace=True)
    body = _body(ProfileSnapshotRequest)
    try:
        is_written = write_workspace_profile_snapshot(_workspace_root(), body.model_dump(mode="json"))
    except InvalidProfileSnapshotError as e:
        raise WorkspaceRouteRefusal(str(e), CODE_INVALID_PROFILE, HTTP_BAD_REQUEST) from e
    except ProfileWriteError as e:
        logger.opt(exception=e).error("Could not write the profile snapshot")
        raise WorkspaceRouteRefusal(str(e), CODE_WRITE_FAILED, HTTP_INTERNAL_ERROR) from e
    return model_response(ProfileWrittenResponse(is_written=is_written))


def write_timezone() -> ResponseReturnValue:
    """Record the user's timezone, by default only when none is recorded, and answer the zone now on record.

    The zone is applied when the container next boots.
    """
    _admit(is_changing_the_workspace=True)
    body = _body(TimezoneRequest)
    workspace_root = _workspace_root()
    try:
        is_written = write_user_timezone(workspace_root, body.zone, body.is_replacing)
    except InvalidTimezoneError as e:
        raise WorkspaceRouteRefusal(str(e), CODE_INVALID_TIMEZONE, HTTP_BAD_REQUEST) from e
    except ProfileWriteError as e:
        logger.opt(exception=e).error("Could not write the user timezone")
        raise WorkspaceRouteRefusal(str(e), CODE_WRITE_FAILED, HTTP_INTERNAL_ERROR) from e
    return model_response(TimezoneResponse(zone=read_user_timezone(workspace_root), is_written=is_written))


# Backups


def _backup_state() -> BackupState:
    return read_backup_state(
        _workspace_root(),
        lease_path=get_state().backup_lease_path,
        now=time.time(),
        workspace_id=workspace_id_from_environment(os.environ),
    )


def read_backups() -> ResponseReturnValue:
    """Everything a client reads about the workspace's backups: where they go, how the setup of that destination
    stands (with what failed and when the workspace tries again), whether restic.env is in place, the
    verification flag, the repository, the digest of the credentials in use, and who holds the backup lease."""
    _admit(is_changing_the_workspace=False)
    return model_response(_backup_state())


def write_backup_destination() -> ResponseReturnValue:
    """Name where the workspace's backups go, with the storage that destination needs where the client has some.

    The workspace sets the destination up itself: it makes the repository's password, initialises the repository,
    installs restic.env, and tries again when that fails. Answers the backup state, under 202 when the request
    left the workspace something to do and under 200 when it did not: a request for what the workspace already
    holds changes nothing. A destination named with no storage is set up from what the workspace kept for it,
    and awaits storage when it kept none. Storage offered for the repository already in use is taken only once
    the workspace finds that the storage refuses what it holds. NONE moves restic.env aside and keeps it.

    A request that would change something while a backup operation runs is refused with 409
    BACKUP_OPERATION_RUNNING, naming who holds the backup lease. A destination the workspace does not know, and
    a password among the variables of the user's own storage, are refused with 400 INVALID_DESTINATION.
    """
    _admit(is_changing_the_workspace=True)
    _body(BackupDestinationRequest)
    # The model masks its secrets when it is dumped, so the library reads the body as it was sent.
    sent = request.get_json(force=True, silent=True)
    body = {str(name): value for name, value in sent.items()} if isinstance(sent, dict) else {}
    try:
        outcome = request_backup_destination(
            _workspace_root(),
            body,
            lease_path=get_state().backup_lease_path,
            now=time.time(),
            workspace_id=workspace_id_from_environment(os.environ),
        )
    except InvalidBackupDestinationError as e:
        raise WorkspaceRouteRefusal(str(e), CODE_INVALID_DESTINATION, HTTP_BAD_REQUEST) from e
    except BackupOperationRunningError as e:
        return model_response(
            BackupOperationRunningRefusal(
                detail=str(e),
                code=CODE_BACKUP_OPERATION_RUNNING,
                lease=BackupLeaseHolder(
                    holder=e.lease.holder, taken_at=e.lease.taken_at, expires_at=e.lease.expires_at
                ),
            ),
            HTTP_CONFLICT,
        )
    except OSError as e:
        logger.opt(exception=e).error("Could not record the backup destination")
        raise WorkspaceRouteRefusal(
            f"The backup destination could not be recorded: {e}", CODE_WRITE_FAILED, HTTP_INTERNAL_ERROR
        ) from e
    logger.info(
        "Asked for the backup destination {}; the setup is {}",
        outcome.state.destination.value,
        outcome.state.setup.state.value,
    )
    return model_response(outcome.state, HTTP_ACCEPTED if outcome.is_setup_begun else HTTP_OK)


def read_backup_credentials_of_workspace() -> ResponseReturnValue:
    """The whole text of the restic.env in use and its digest, both '' when there is none, to the owner alone.

    The workspace makes the repository's password, so this is how an owner's client comes to hold a copy of it:
    it reads the credentials when the digest the backup state answers differs from its own copy's. Anyone but the
    owner is refused with 403 OWNER_ONLY.
    """
    _admit(is_changing_the_workspace=False)
    identity = request_identity()
    if not identity.owner or identity.agent_id is not None:
        raise WorkspaceRouteRefusal(_OWNER_ONLY_REFUSAL_DETAIL, CODE_OWNER_ONLY, HTTP_FORBIDDEN)
    return model_response(read_backup_credentials(_workspace_root()))


def write_backup_verification() -> ResponseReturnValue:
    """Turn backup verification on or off for this workspace, and answer the value now set."""
    _admit(is_changing_the_workspace=True)
    body = _body(BackupVerificationModel)
    workspace_root = _workspace_root()
    try:
        write_verification_disabled(workspace_root, is_disabled=not body.is_enabled)
    except OSError as e:
        logger.opt(exception=e).error("Could not write the backup verification flag")
        raise WorkspaceRouteRefusal(
            f"The backup verification flag could not be written: {e}", CODE_WRITE_FAILED, HTTP_INTERNAL_ERROR
        ) from e
    return model_response(BackupVerificationModel(is_enabled=not read_verification_disabled(workspace_root)))


# Updates


def _require_workspace_root_as_working_directory() -> None:
    """The update library acts on the workspace at the working directory, as the dispatcher does under cron, which
    is where supervisord runs the shell; a shell run from anywhere else must not write another tree's state."""
    if Path.cwd().resolve() != _workspace_root().resolve():
        raise WorkspaceRouteRefusal(
            "The shell is not running from the workspace's root, so it cannot act on the workspace's updates.",
            CODE_NOT_AT_WORKSPACE_ROOT,
            HTTP_INTERNAL_ERROR,
        )


def _observed_agents_reader() -> AgentsReader:
    events_path = str(get_state().shell.avatar_status.events_path)
    return lambda: read_observed_agents(events_path, time.time())


def _outcome_response(answer: dict[str, object]) -> UpdateOutcomeResponse:
    return UpdateOutcomeResponse.model_validate(
        {key: value for key, value in answer.items() if key in UpdateOutcomeResponse.model_fields}
    )


def _window_of(body: UpdateScheduleRefreshRequest) -> tuple[int, int] | None:
    """The window a request named, or None when it left the window to the workspace; a request that names half
    of one is refused."""
    if body.window_start_minute_utc is None and body.window_length_minutes is None:
        return None
    if body.window_start_minute_utc is None or body.window_length_minutes is None:
        raise WorkspaceRouteRefusal(
            "window_start_minute_utc and window_length_minutes come together, or not at all.",
            CODE_INVALID_REQUEST,
            HTTP_BAD_REQUEST,
        )
    return body.window_start_minute_utc, body.window_length_minutes


def write_update_settings_of_workspace() -> ResponseReturnValue:
    """Store the update settings: the release channel the workspace follows, where that channel's file is read
    from, whether the workspace arms its own updates, and the hours of the owner's day one may start in.

    Answers the settings as stored. The dispatcher reads them every minute: it reads the channel's version at
    most once an hour, and while arming is on it arms an update to that version by itself. The hours are read in
    the timezone the workspace records, and in UTC when it records none.
    """
    _admit(is_changing_the_workspace=True)
    body = _body(UpdateSettingsModel)
    _require_workspace_root_as_working_directory()
    try:
        stored = write_update_settings(UpdateSettings(**body.model_dump()))
    except InvalidUpdateSettingsError as e:
        raise WorkspaceRouteRefusal(str(e), CODE_INVALID_UPDATE_SETTINGS, HTTP_BAD_REQUEST) from e
    except OSError as e:
        logger.opt(exception=e).error("Could not write the update settings")
        raise WorkspaceRouteRefusal(
            f"The update settings could not be written: {e}", CODE_WRITE_FAILED, HTTP_INTERNAL_ERROR
        ) from e
    return model_response(UpdateSettingsModel.model_validate(stored.document()))


def read_update() -> ResponseReturnValue:
    """The schedule, the last run, the last dispatch, the last start, the skipped version, the dismissals, the
    update settings, and the version the settings' release channel is on as the workspace last read it.

    Nothing in the workspace changes, and the feed is not read. ``?check_sign_in=1`` also asks whether an AI
    account is signed in.
    """
    _admit(is_changing_the_workspace=False)
    _require_workspace_root_as_working_directory()
    state = read_update_state(
        is_sign_in_checked=request.args.get(CHECK_SIGN_IN_PARAMETER) == "1",
        proc_root=PROC_ROOT,
        agent_environment=AgentEnvironment(),
        read_agents=_observed_agents_reader(),
    )
    last_start = state["last_start"]
    channel_version = state["channel_version"]
    return model_response(
        UpdateStateResponse.model_validate(
            {
                **state,
                "channel_version": UpdateChannelVersion.model_validate(
                    {key: value for key, value in channel_version.items() if key in UpdateChannelVersion.model_fields}
                )
                if isinstance(channel_version, dict)
                else None,
                "last_start": UpdateStartAttempt.model_validate(
                    {key: value for key, value in last_start.items() if key in UpdateStartAttempt.model_fields}
                )
                if isinstance(last_start, dict)
                else None,
            }
        )
    )


def start_update_now() -> ResponseReturnValue:
    """Start an update now.

    The quick checks come first (the update-self skill is there, an account is signed in, no update is running or
    about to), and one that fails is answered 200 with its outcome. Otherwise the update's chat begins to be
    created and the answer is 202 with the outcome STARTING and an attempt id; the read route's ``last_start``
    holds the attempt's outcome once it has one.

    A request that names no ``ceiling`` updates to the version the workspace's release channel is on, and is
    answered NO_CHANNEL_VERSION when the workspace has read none and the user named none.
    """
    _admit(is_changing_the_workspace=True)
    body = _body(UpdateStartRequest)
    _require_workspace_root_as_working_directory()
    answer = start_update(
        ceiling=body.ceiling,
        override_ref=body.override_ref,
        is_backup_configured=body.is_backup_configured,
        now=time.time(),
        live_cron_dir=str(get_state().live_cron_directory),
        proc_root=PROC_ROOT,
        agent_environment=AgentEnvironment(),
        read_agents=_observed_agents_reader(),
        run_in_background=_run_in_background,
        clock=time.time,
        fetch_channel_file=get_state().channel_file_fetcher,
    )
    logger.info(
        "Asked to start an update to {}: {}",
        body.override_ref or body.ceiling or "the channel's version",
        answer.get("outcome"),
    )
    response = _outcome_response(answer)
    return model_response(response, HTTP_ACCEPTED if response.outcome == OUTCOME_STARTING else HTTP_OK)


def arm_update_schedule() -> ResponseReturnValue:
    """Arm the schedule: a scheduled update starts inside the window, by the dispatcher cron runs every minute.

    Answers the outcome (ARMED, or why not: NO_CRON, TOO_OLD_TO_UPDATE, NEEDS_SIGN_IN) and the schedule. Arming
    again starts the dispatcher's record of earlier attempts over.

    A value the request leaves out is the workspace's own, which the dispatcher then keeps current in the
    schedule: the version its release channel is on (NO_CHANNEL_VERSION when it has read none and the user named
    none), the window of its update settings (NO_UPDATE_SETTINGS when it holds none), and whether it holds backup
    credentials.
    """
    _admit(is_changing_the_workspace=True)
    body = _body(UpdateScheduleRequest)
    window = _window_of(body)
    _require_workspace_root_as_working_directory()
    answer = arm_schedule(
        ceiling=body.ceiling,
        override_ref=body.override_ref,
        window_start_minute_utc=window[0] if window is not None else None,
        window_length_minutes=window[1] if window is not None else None,
        is_backup_configured=body.is_backup_configured,
        is_auto_armed=body.is_auto_armed,
        now=time.time(),
        live_cron_dir=str(get_state().live_cron_directory),
        agent_environment=AgentEnvironment(),
        fetch_channel_file=get_state().channel_file_fetcher,
    )
    logger.info("Asked to arm an update schedule: {}", answer.get("outcome"))
    return model_response(_outcome_response(answer))


def refresh_update_schedule() -> ResponseReturnValue:
    """Rewrite the client's own values in an armed schedule (the ceiling, the window, whether backups are
    configured), and nothing when none is armed. The version the user typed and the dispatcher's record stay.

    A value the request leaves out is rewritten with the workspace's own, and left as it is where the workspace
    has none.
    """
    _admit(is_changing_the_workspace=True)
    body = _body(UpdateScheduleRefreshRequest)
    window = _window_of(body)
    _require_workspace_root_as_working_directory()
    answer = refresh_schedule(
        ceiling=body.ceiling,
        window_start_minute_utc=window[0] if window is not None else None,
        window_length_minutes=window[1] if window is not None else None,
        is_backup_configured=body.is_backup_configured,
        now=time.time(),
        live_cron_dir=str(get_state().live_cron_directory),
        fetch_channel_file=get_state().channel_file_fetcher,
    )
    return model_response(UpdateScheduleRefreshedResponse.model_validate(answer))


def cancel_update_schedule() -> ResponseReturnValue:
    """Cancel the schedule, and answer whether there was one."""
    _admit(is_changing_the_workspace=True)
    _require_workspace_root_as_working_directory()
    return model_response(UpdateScheduleCancelledResponse.model_validate(cancel_schedule()))


def skip_update() -> ResponseReturnValue:
    """Cancel the schedule and record a version as skipped for this workspace; of two skips the later is kept."""
    _admit(is_changing_the_workspace=True)
    body = _body(UpdateSkipRequest)
    _require_workspace_root_as_working_directory()
    answer = skip_target(target_ref=body.target_ref, skipped_at=body.skipped_at)
    return model_response(UpdateSkippedResponse.model_validate(answer))


def dismiss_update() -> ResponseReturnValue:
    """Record a run's note or outcome as dismissed, by the run's start; a later run's start covers an earlier one's."""
    _admit(is_changing_the_workspace=True)
    body = _body(UpdateDismissalsRequest)
    _require_workspace_root_as_working_directory()
    answer = record_dismissals(
        note_run_started_at=body.note_run_started_at, outcome_run_started_at=body.outcome_run_started_at
    )
    return model_response(UpdateDismissalsResponse.model_validate(answer))


def read_update_liveness() -> ResponseReturnValue:
    """What tells whether a running update is still alive: the run record, the workspace's agents with their states
    and chats, the chats waiting on background tasks, and whether the dispatcher's cron line is installed."""
    _admit(is_changing_the_workspace=False)
    _require_workspace_root_as_working_directory()
    liveness = read_liveness(
        live_cron_dir=str(get_state().live_cron_directory),
        agent_environment=AgentEnvironment(),
        read_agents=_observed_agents_reader(),
    )
    agents = liveness["agents"]
    busy_chats = liveness["busy_chats"]
    return model_response(
        UpdateLivenessResponse(
            run=liveness["run"] if isinstance(liveness["run"], dict) else None,
            agents=tuple(UpdateLivenessAgent.model_validate(agent) for agent in agents)
            if isinstance(agents, list)
            else None,
            busy_chats=tuple(UpdateBusyChat.model_validate(row) for row in busy_chats)
            if isinstance(busy_chats, list)
            else (),
            is_watchdog_installed=liveness["is_watchdog_installed"] is True,
        )
    )


def _route(
    request_model: type[BaseModel] | None,
    response_model: type[BaseModel],
    success_status: int = HTTP_OK,
    other_answers: tuple[AlternateResponse, ...] = (),
) -> RouteModels:
    """A workspace route's models: what it takes, what it answers, and the error object under each failure status."""
    return RouteModels(
        request_model=request_model,
        response_model=response_model,
        success_status=success_status,
        alternate_responses=(
            *other_answers,
            *(AlternateResponse(status=status, model=WorkspaceRouteError) for status in _ERROR_STATUSES),
        ),
    )


WORKSPACE_ROUTE_TABLE: RouteTable = {
    ("GET", STATUS_ROUTE): _route(None, WorkspaceStatusResponse),
    ("GET", SHARING_ROUTE): _route(None, SharingStateResponse),
    ("PUT", SHARING_GRANTS_ROUTE): _route(
        ReplaceGrantsRequest,
        GrantsStoredResponse,
        other_answers=(AlternateResponse(status=HTTP_BAD_REQUEST, model=InvalidGrantsError),),
    ),
    ("PUT", PROFILE_ROUTE): _route(ProfileSnapshotRequest, ProfileWrittenResponse),
    ("PUT", TIMEZONE_ROUTE): _route(TimezoneRequest, TimezoneResponse),
    ("GET", BACKUPS_ROUTE): _route(None, BackupState),
    ("PUT", BACKUP_DESTINATION_ROUTE): _route(
        BackupDestinationRequest,
        BackupState,
        success_status=HTTP_ACCEPTED,
        other_answers=(
            AlternateResponse(status=HTTP_OK, model=BackupState),
            AlternateResponse(status=HTTP_CONFLICT, model=BackupOperationRunningRefusal),
        ),
    ),
    ("GET", BACKUP_CREDENTIALS_ROUTE): _route(None, BackupCredentials),
    ("PUT", BACKUP_VERIFICATION_ROUTE): _route(BackupVerificationModel, BackupVerificationModel),
    ("GET", UPDATE_ROUTE): _route(None, UpdateStateResponse),
    ("POST", UPDATE_START_ROUTE): _route(
        UpdateStartRequest,
        UpdateOutcomeResponse,
        other_answers=(AlternateResponse(status=HTTP_ACCEPTED, model=UpdateOutcomeResponse),),
    ),
    ("PUT", UPDATE_SCHEDULE_ROUTE): _route(UpdateScheduleRequest, UpdateOutcomeResponse),
    ("PATCH", UPDATE_SCHEDULE_ROUTE): _route(UpdateScheduleRefreshRequest, UpdateScheduleRefreshedResponse),
    ("DELETE", UPDATE_SCHEDULE_ROUTE): _route(None, UpdateScheduleCancelledResponse),
    ("POST", UPDATE_SKIP_ROUTE): _route(UpdateSkipRequest, UpdateSkippedResponse),
    ("POST", UPDATE_DISMISSALS_ROUTE): _route(UpdateDismissalsRequest, UpdateDismissalsResponse),
    ("GET", UPDATE_LIVENESS_ROUTE): _route(None, UpdateLivenessResponse),
    ("PUT", UPDATE_SETTINGS_ROUTE): _route(UpdateSettingsModel, UpdateSettingsModel),
}

# The operation each route serves, as the status route names it; the status route itself serves none.
WORKSPACE_OPERATION_BY_ROUTE: Final[dict[tuple[str, str], str]] = {
    ("GET", SHARING_ROUTE): OPERATION_SHARING_READ,
    ("PUT", SHARING_GRANTS_ROUTE): OPERATION_SHARING_GRANTS_WRITE,
    ("PUT", PROFILE_ROUTE): OPERATION_PROFILE_WRITE,
    ("PUT", TIMEZONE_ROUTE): OPERATION_TIMEZONE_WRITE,
    ("GET", BACKUPS_ROUTE): OPERATION_BACKUPS_READ,
    ("PUT", BACKUP_DESTINATION_ROUTE): OPERATION_BACKUPS_DESTINATION_WRITE,
    ("GET", BACKUP_CREDENTIALS_ROUTE): OPERATION_BACKUPS_CREDENTIALS_READ,
    ("PUT", BACKUP_VERIFICATION_ROUTE): OPERATION_BACKUPS_VERIFICATION_WRITE,
    ("GET", UPDATE_ROUTE): OPERATION_UPDATE_READ,
    ("POST", UPDATE_START_ROUTE): OPERATION_UPDATE_START,
    ("PUT", UPDATE_SCHEDULE_ROUTE): OPERATION_UPDATE_SCHEDULE_WRITE,
    ("PATCH", UPDATE_SCHEDULE_ROUTE): OPERATION_UPDATE_SCHEDULE_REFRESH,
    ("DELETE", UPDATE_SCHEDULE_ROUTE): OPERATION_UPDATE_SCHEDULE_CANCEL,
    ("POST", UPDATE_SKIP_ROUTE): OPERATION_UPDATE_SKIP,
    ("POST", UPDATE_DISMISSALS_ROUTE): OPERATION_UPDATE_DISMISS,
    ("GET", UPDATE_LIVENESS_ROUTE): OPERATION_UPDATE_LIVENESS_READ,
    ("PUT", UPDATE_SETTINGS_ROUTE): OPERATION_UPDATE_SETTINGS_WRITE,
}

_VIEW_BY_ROUTE: Final[dict[tuple[str, str], Callable[[], ResponseReturnValue]]] = {
    ("GET", STATUS_ROUTE): workspace_status,
    ("GET", SHARING_ROUTE): read_sharing,
    ("PUT", SHARING_GRANTS_ROUTE): replace_sharing_grants,
    ("PUT", PROFILE_ROUTE): write_profile,
    ("PUT", TIMEZONE_ROUTE): write_timezone,
    ("GET", BACKUPS_ROUTE): read_backups,
    ("PUT", BACKUP_DESTINATION_ROUTE): write_backup_destination,
    ("GET", BACKUP_CREDENTIALS_ROUTE): read_backup_credentials_of_workspace,
    ("PUT", BACKUP_VERIFICATION_ROUTE): write_backup_verification,
    ("GET", UPDATE_ROUTE): read_update,
    ("POST", UPDATE_START_ROUTE): start_update_now,
    ("PUT", UPDATE_SCHEDULE_ROUTE): arm_update_schedule,
    ("PATCH", UPDATE_SCHEDULE_ROUTE): refresh_update_schedule,
    ("DELETE", UPDATE_SCHEDULE_ROUTE): cancel_update_schedule,
    ("POST", UPDATE_SKIP_ROUTE): skip_update,
    ("POST", UPDATE_DISMISSALS_ROUTE): dismiss_update,
    ("GET", UPDATE_LIVENESS_ROUTE): read_update_liveness,
    ("PUT", UPDATE_SETTINGS_ROUTE): write_update_settings_of_workspace,
}


def register_workspace_routes(application: Flask) -> None:
    """Register every workspace route on ``application``."""
    application.register_error_handler(WorkspaceRouteRefusal, _answer_refusal)
    for (method, path), view in _VIEW_BY_ROUTE.items():
        endpoint = f"workspace_{method.lower()}{path.removeprefix(WORKSPACE_ROUTE_PREFIX).replace('/', '_')}"
        application.add_url_rule(path, view_func=view, methods=[method], endpoint=endpoint)

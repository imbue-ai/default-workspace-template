"""The request and answer models of the workspace routes (``/api/workspace/...``): the state a workspace owns, as a
client outside it reads and changes it.

A client is the Imbue Studio desktop's backend or the hosted web client. Every request carries every value the route
acts on, so a client that keeps nothing between requests can call it. The routes' description is checked in beside
the app (``workspace_routes_api.json``), and a client is held to it: a path, a field of an answer, and the meaning of
an operation's name never change while a client that uses them is supported.
"""

from typing import Final

from app_schema.api_schema import ErrorResponse
from pydantic import ConfigDict
from pydantic import Field
from pydantic import StrictBool
from pydantic import StrictFloat
from pydantic import StrictInt
from workspace_backup_setup.state import BackupLeaseHolder
from workspace_initial_data.status import InitialDataStatus

from imbue.imbue_common.frozen_model import FrozenModel

WORKSPACE_STATUS_SCHEMA: Final[int] = 1

# The template's compatibility number, which the status route answers. A client is built with the highest number
# it knows, and one that reads a higher number still opens the workspace and runs its lifecycle, and refuses
# everything else it would do inside it rather than fall back to an older path. It is raised only by a change a
# client built before it could not work with: a route that changes in a way the frozen descriptions forbid
# (``workspace_routes_api_frozen/``), a format a client's own programs write, or a page the embed contract cannot
# carry. A change every supported client still works with leaves it alone, and it is never lowered.
WORKSPACE_COMPATIBILITY: Final[int] = 1

# The operations a workspace on this template serves, each named for good: a release adds names and never renames
# one. A client calls a route only for an operation its workspace names.
OPERATION_SHARING_READ: Final[str] = "sharing.read"
OPERATION_SHARING_GRANTS_WRITE: Final[str] = "sharing.grants.write"
OPERATION_PROFILE_WRITE: Final[str] = "profile.write"
OPERATION_TIMEZONE_WRITE: Final[str] = "timezone.write"
OPERATION_BACKUPS_READ: Final[str] = "backups.read"
OPERATION_BACKUPS_DESTINATION_WRITE: Final[str] = "backups.destination.write"
OPERATION_BACKUPS_CREDENTIALS_READ: Final[str] = "backups.credentials.read"
OPERATION_BACKUPS_VERIFICATION_WRITE: Final[str] = "backups.verification.write"
OPERATION_UPDATE_READ: Final[str] = "update.read"
OPERATION_UPDATE_START: Final[str] = "update.start"
OPERATION_UPDATE_SCHEDULE_WRITE: Final[str] = "update.schedule.write"
OPERATION_UPDATE_SCHEDULE_CANCEL: Final[str] = "update.schedule.cancel"
OPERATION_UPDATE_SCHEDULE_REFRESH: Final[str] = "update.schedule.refresh"
OPERATION_UPDATE_SKIP: Final[str] = "update.skip"
OPERATION_UPDATE_DISMISS: Final[str] = "update.dismiss"
OPERATION_UPDATE_LIVENESS_READ: Final[str] = "update.liveness.read"
OPERATION_UPDATE_SETTINGS_WRITE: Final[str] = "update.settings.write"
OPERATION_SUGGESTIONS_READ: Final[str] = "suggestions.read"
OPERATION_SUGGESTIONS_WRITE: Final[str] = "suggestions.write"
OPERATION_APPS_READ: Final[str] = "apps.read"
OPERATION_WALLPAPER_READ: Final[str] = "wallpaper.read"

WORKSPACE_OPERATIONS: Final[tuple[str, ...]] = (
    OPERATION_SHARING_READ,
    OPERATION_SHARING_GRANTS_WRITE,
    OPERATION_PROFILE_WRITE,
    OPERATION_TIMEZONE_WRITE,
    OPERATION_BACKUPS_READ,
    OPERATION_BACKUPS_DESTINATION_WRITE,
    OPERATION_BACKUPS_CREDENTIALS_READ,
    OPERATION_BACKUPS_VERIFICATION_WRITE,
    OPERATION_UPDATE_READ,
    OPERATION_UPDATE_START,
    OPERATION_UPDATE_SCHEDULE_WRITE,
    OPERATION_UPDATE_SCHEDULE_CANCEL,
    OPERATION_UPDATE_SCHEDULE_REFRESH,
    OPERATION_UPDATE_SKIP,
    OPERATION_UPDATE_DISMISS,
    OPERATION_UPDATE_LIVENESS_READ,
    OPERATION_UPDATE_SETTINGS_WRITE,
    OPERATION_SUGGESTIONS_READ,
    OPERATION_SUGGESTIONS_WRITE,
    OPERATION_APPS_READ,
    OPERATION_WALLPAPER_READ,
)

# The codes a workspace route's refusal or failure carries beside its ``detail``, for a caller that acts on it.
CODE_INVALID_REQUEST: Final[str] = "INVALID_REQUEST"
CODE_PREVIEW: Final[str] = "PREVIEW"
CODE_INVALID_GRANTS: Final[str] = "INVALID_GRANTS"
CODE_GRANTS_UNREADABLE: Final[str] = "GRANTS_UNREADABLE"
CODE_INVALID_PROFILE: Final[str] = "INVALID_PROFILE"
CODE_INVALID_TIMEZONE: Final[str] = "INVALID_TIMEZONE"
CODE_INVALID_DESTINATION: Final[str] = "INVALID_DESTINATION"
CODE_BACKUP_OPERATION_RUNNING: Final[str] = "BACKUP_OPERATION_RUNNING"
CODE_OWNER_ONLY: Final[str] = "OWNER_ONLY"
CODE_INVALID_UPDATE_SETTINGS: Final[str] = "INVALID_UPDATE_SETTINGS"
CODE_WRITE_FAILED: Final[str] = "WRITE_FAILED"
CODE_SUGGESTIONS_UNAVAILABLE: Final[str] = "SUGGESTIONS_UNAVAILABLE"
CODE_SUGGESTIONS_UNSUPPORTED: Final[str] = "SUGGESTIONS_UNSUPPORTED"
CODE_NOT_AT_WORKSPACE_ROOT: Final[str] = "NOT_AT_WORKSPACE_ROOT"

GRANTS_ERROR_UNREADABLE: Final[str] = "UNREADABLE"
# The key a grants document holds its per-app lists under, on the wire as in the grants file.
GRANTS_PER_APP_KEY: Final[str] = "services"


class WorkspaceRouteError(ErrorResponse):
    """What a workspace route answers when it refuses a request (4xx) or could not do what it was asked (5xx)."""

    code: str = Field(description="An upper-case code naming the refusal or failure, for a caller that acts on it")


class GrantRefusalEntry(FrozenModel):
    """One entry a grants document may not carry, and why."""

    scope: str = Field(description="'workspace', or the name of the app whose list carried the entry")
    kind: str = Field(description="'user', 'email' or 'email_domain'")
    value: str = Field(description="The entry exactly as the caller sent it")
    message: str = Field(description="Why the entry was refused")


class InvalidGrantsError(WorkspaceRouteError):
    """A grants document whose shape is wrong: every entry refused, so a client can mark each row."""

    refusals: tuple[GrantRefusalEntry, ...] = Field(description="The entries refused, workspace scope first")


class WorkspaceStatusResponse(FrozenModel):
    """What the workspace serves: the version of its checkout, the operations it has a route for, the
    compatibility number a client checks before it acts inside the workspace, and how the initial data it was
    handed at create stands."""

    model_config = ConfigDict(serialize_by_alias=True)

    status_schema: int = Field(alias="schema", description="The shape of this answer; 1")
    template_ref: str = Field(
        description="The version of the workspace's checkout: the ref its newest update-self merge names, or its "
        "nearest minds-v* tag when there is no such merge or the tag is a newer release; '' when it names none"
    )
    operations: tuple[str, ...] = Field(description="The name of every operation this workspace serves a route for")
    compatibility: int = Field(
        description="The template's compatibility number, raised only by a change an older client could not work "
        "with; an answer without it means 1. A client that knows a lower number opens the workspace and runs its "
        "lifecycle, and does nothing else inside it"
    )
    initial_data: InitialDataStatus = Field(
        description="How the document the workspace was handed when it was created stands: whether its boot "
        "found and applied one. An answer without it is a template from before the initial data, which ignores "
        "the document"
    )


class GrantListModel(FrozenModel):
    """One sharing scope's allow-list: user ids, exact emails (invitations), and whole email domains."""

    users: tuple[str, ...] = Field(default=(), description="User ids granted access (matched before emails)")
    emails: tuple[str, ...] = Field(
        default=(), description="Exact email addresses granted access; the gateway turns each into a user id"
    )
    email_domains: tuple[str, ...] = Field(default=(), description="Whole email domains granted access")


class GrantsDocumentModel(FrozenModel):
    """The whole grants document, in the shape of ``share_grants.toml``."""

    model_config = ConfigDict(serialize_by_alias=True)

    workspace: GrantListModel = Field(
        default_factory=GrantListModel, description="Who is admitted to the whole workspace"
    )
    # Named for the grants file's own word for its per-app tables on the wire.
    apps: dict[str, GrantListModel] = Field(
        default_factory=dict,
        alias=GRANTS_PER_APP_KEY,
        description="Who is admitted to one app alone, by the app's name",
    )


class ShareableApp(FrozenModel):
    """One app of the workspace that can be shared by itself."""

    name: str = Field(description="The app's registered name, which its grants are keyed by")
    label: str = Field(
        description="The app's origin label, the first part of its share address; '' for an app that has none yet"
    )
    display_name: str = Field(description="What a person reads for the app; '' when it registered without one")
    is_system: bool = Field(
        description="Whether the app is part of the workspace itself rather than built on it, which a sharing panel "
        "lists apart from the apps it offers up front"
    )


class SharingStateResponse(FrozenModel):
    """Whether the workspace holds share materials, its grants, and the apps that can be shared."""

    is_shared: bool = Field(description="Whether share.env exists, the share stack's on-switch")
    grants: GrantsDocumentModel | None = Field(
        description="The grants document; null when there is no grants file, or it holds something else, so that a "
        "save replaces it"
    )
    grants_error: str = Field(
        description="'UNREADABLE' when the grants file is there and cannot be read; '' otherwise"
    )
    shareable_apps: tuple[ShareableApp, ...] = Field(
        description="The registered apps that are not internal, in the registry's order: each can be shared by "
        "itself, and an internal app is reached only through a share of the whole workspace"
    )


class WorkspaceApp(ShareableApp):
    """One app of the workspace, as a client lists it and draws it."""

    icon: str = Field(
        description="The SVG markup the app registered as its icon, as it registered it; '' when it has none. It "
        "is the app's own markup, so a client draws it as an image and never as part of its page"
    )


class WorkspaceAppsResponse(FrozenModel):
    """The workspace's apps."""

    apps: tuple[WorkspaceApp, ...] = Field(
        description="The registered apps that are not internal, in the registry's order: the apps the sharing "
        "read lists as shareable. Those with is_system false are the apps built on the workspace, which a client "
        "lists as the workspace's own"
    )


class GrantedIdentityModel(FrozenModel):
    """What the client knows of one granted account: what the shell names it from before it first arrives."""

    email: str | None = Field(default=None, description="The account's email; null when the client knows none")
    display_name: str | None = Field(
        default=None, description="The account's display name; null when the client knows none"
    )


class ReplaceGrantsRequest(FrozenModel):
    """The grants to store, whole, with what the client knows of each granted account."""

    grants: GrantsDocumentModel = Field(description="The whole grants document")
    identities: dict[str, GrantedIdentityModel] = Field(
        default_factory=dict, description="Each granted account's email and display name, by user id"
    )


class GrantsStoredResponse(FrozenModel):
    """The grants document as it was stored: trimmed, lowercased, and each list in order."""

    grants: GrantsDocumentModel = Field(description="The stored document")


class ProfileSnapshotRequest(FrozenModel):
    """The profile snapshot to write, as it is: its header fields, and whatever settings it carries beside them."""

    model_config = ConfigDict(extra="allow")

    snapshot_format: StrictInt = Field(
        description="The snapshot's shape version; one newer than this template is kept"
    )


class ProfileWrittenResponse(FrozenModel):
    """What writing the profile snapshot answers."""

    is_written: bool = Field(
        description="Whether the snapshot was written; false when the one stored already equals the one sent apart "
        "from when it was written, which is then left as it is"
    )


class TimezoneRequest(FrozenModel):
    """The user's timezone to record."""

    zone: str = Field(description="An IANA timezone name")
    is_replacing: StrictBool = Field(
        default=False, description="Whether to replace a zone already on record; false writes only when there is none"
    )


class TimezoneResponse(FrozenModel):
    """The timezone now on record, applied when the container next boots."""

    zone: str = Field(description="The zone now recorded")
    is_written: bool = Field(description="Whether this request wrote it")


class BackupOperationRunningRefusal(WorkspaceRouteError):
    """A change of the backup destination refused because a backup operation is running."""

    lease: BackupLeaseHolder = Field(description="Who holds the backup lease, and until when")


class BackupVerificationModel(FrozenModel):
    """Whether a client checks this workspace's backups."""

    is_enabled: StrictBool = Field(description="Whether backup verification is on")


class UpdateSettingsModel(FrozenModel):
    """The update settings a workspace holds: the release channel it follows, and when it arms its own updates.

    Three are the workspace's own, which its owner sets in the workspace's settings: its own answer on arming, and
    its own hours. A request that leaves one of them out keeps the stored value, and one that sends null clears it,
    so a client that writes the account's settings, or one from before a setting existed, cannot wipe an answer the
    owner gave in another client. An answer carries every one of them.
    """

    channel: str = Field(min_length=1, description="The release channel the workspace follows")
    feed_base_url: str = Field(
        description="Where that channel's file is read from (<feed_base_url>/<channel>-web.json); '' on a tier that "
        "publishes none, where the workspace reads no version"
    )
    is_auto_update_enabled: StrictBool = Field(description="Whether the account's workspaces arm their own updates")
    is_auto_update_enabled_here: StrictBool | None = Field(
        default=None,
        description="This workspace's own answer, which wins over the account's; null when it has none. Left out "
        "of a request, the stored answer is kept",
    )
    window_start_hour: StrictInt = Field(
        ge=0, le=23, description="The hour of the owner's day an armed update may start from, as the account sets it"
    )
    window_end_hour: StrictInt = Field(
        ge=0, le=23, description="The hour it may no longer start at; an earlier hour than the start is the next day's"
    )
    window_start_hour_here: StrictInt | None = Field(
        default=None,
        ge=0,
        le=23,
        description="This workspace's own start hour, which with its end hour wins over the account's; both are "
        "set, or both null when it has none. Left out of a request, the stored hour is kept",
    )
    window_end_hour_here: StrictInt | None = Field(
        default=None,
        ge=0,
        le=23,
        description="This workspace's own end hour; set or null with its start hour. Left out of a request, the "
        "stored hour is kept",
    )


class SuggestionSettingsRequest(FrozenModel):
    """The suggestion switches to change. One left out, or null, stays as it is."""

    is_next_step_suggestions_enabled: StrictBool | None = Field(
        default=None, description="Whether suggested replies are offered under an agent's reply"
    )
    is_new_chat_suggestions_enabled: StrictBool | None = Field(
        default=None, description="Whether things to try are offered on a chat with no message yet"
    )


class SuggestionSettingsResponse(FrozenModel):
    """The workspace's two suggestion switches, as they stand."""

    is_next_step_suggestions_enabled: bool = Field(
        description="Whether suggested replies are offered under an agent's reply"
    )
    is_new_chat_suggestions_enabled: bool = Field(
        description="Whether things to try are offered on a chat with no message yet"
    )


class UpdateStartRequest(FrozenModel):
    """The values an update started now runs with. Each one left out is the workspace's own."""

    ceiling: str | None = Field(
        default=None,
        min_length=1,
        description="The newest template version the client knows; an update goes no further. Left out, it is the "
        "version the workspace's release channel is on, and only a client from before the workspace read that "
        "itself names one",
    )
    override_ref: str = Field(default="", description="A version the user typed; the target is this, or the ceiling")
    is_backup_configured: StrictBool | None = Field(
        default=None,
        description="Whether the client holds backup credentials for the workspace. Left out, it is whether the "
        "workspace holds them",
    )


class UpdateScheduleRefreshRequest(FrozenModel):
    """The values a refresh rewrites in an armed schedule. Each one left out is rewritten with the workspace's own."""

    ceiling: str | None = Field(
        default=None,
        min_length=1,
        description="The newest template version the client knows; an update goes no further. Left out, it is the "
        "version the workspace's release channel is on, and only a client from before the workspace read that "
        "itself names one",
    )
    window_start_minute_utc: StrictInt | None = Field(
        default=None,
        ge=0,
        lt=24 * 60,
        description="Minutes past UTC midnight the update window opens at. Left out with the length, the window is "
        "the update settings', in the workspace's timezone",
    )
    window_length_minutes: StrictInt | None = Field(
        default=None,
        ge=0,
        lt=24 * 60,
        description="How long the window stays open; given with the start or not at all",
    )
    is_backup_configured: StrictBool | None = Field(
        default=None,
        description="Whether the client holds backup credentials for the workspace. Left out, it is whether the "
        "workspace holds them",
    )


class UpdateScheduleRequest(UpdateScheduleRefreshRequest):
    """Everything a schedule is armed with. A value left out is the workspace's own, which it then keeps current."""

    override_ref: str = Field(default="", description="A version the user typed; the target is this, or the ceiling")
    is_auto_armed: StrictBool = Field(
        default=False, description="Whether the client armed the schedule by itself, rather than the user"
    )


class UpdateSkipRequest(FrozenModel):
    """The version the user chose to skip for this workspace."""

    target_ref: str = Field(min_length=1, description="The version skipped")
    skipped_at: StrictFloat = Field(
        allow_inf_nan=False,
        description="When the user chose it, seconds since the epoch; of two skips the later is kept",
    )


class UpdateDismissalsRequest(FrozenModel):
    """Which run's note or outcome the user dismissed, by the run's start."""

    note_run_started_at: StrictFloat | None = Field(
        default=None,
        allow_inf_nan=False,
        description="The start of the run whose note was dismissed, seconds since the epoch",
    )
    outcome_run_started_at: StrictFloat | None = Field(
        default=None,
        allow_inf_nan=False,
        description="The start of the run whose outcome was dismissed, seconds since the epoch",
    )


class UpdateOutcomeResponse(FrozenModel):
    """What starting an update or arming a schedule came to.

    ``outcome`` is one of ``STARTING``, ``CREATED``, ``ARMED``, ``ALREADY_RUNNING``, ``NEEDS_SIGN_IN``,
    ``CHAT_APP_UNREACHABLE``, ``CREATE_FAILED``, ``NO_CRON`` and ``TOO_OLD_TO_UPDATE``; a later template may add one.
    A request that left its version or its window to the workspace may also be answered ``NO_CHANNEL_VERSION`` (the
    workspace has read no version for its release channel) or ``NO_UPDATE_SETTINGS`` (it holds no update settings).
    """

    outcome: str = Field(description="What the request came to")
    detail: str = Field(default="", description="One line about it")
    chat_agent_name: str = Field(default="", description="The update's chat an ALREADY_RUNNING names; '' otherwise")
    chat_id: str = Field(default="", description="That chat's id, for opening it; '' when unknown")
    run_started_at: float | None = Field(
        default=None, description="When an ALREADY_RUNNING run began, seconds since the epoch; null when unsaid"
    )
    attempt_id: str = Field(
        default="", description="The start attempt a STARTING began (or one already under way); '' otherwise"
    )
    schedule: dict[str, object] | None = Field(
        default=None, description="The schedule file as arming left it; null for a start"
    )


class UpdateStartAttempt(FrozenModel):
    """The last attempt at starting an update now, and how creating its chat went."""

    attempt_id: str = Field(description="The attempt's id, which the start answered")
    outcome: str | None = Field(
        description="CREATED, CHAT_APP_UNREACHABLE, CREATE_FAILED or NEEDS_SIGN_IN once the attempt has ended; null "
        "while the chat is being created"
    )
    detail: str = Field(default="", description="One line about the outcome")
    chat_agent_name: str = Field(default="", description="The update's chat, once created")
    chat_id: str = Field(default="", description="That chat's id; '' when unknown")
    started_at: float | None = Field(default=None, description="When the attempt began, seconds since the epoch")
    finished_at: float | None = Field(default=None, description="When it ended; null while it runs")


class UpdateChannelVersion(FrozenModel):
    """The version the workspace's release channel is on, as the workspace last read it off the feed."""

    channel: str = Field(description="The channel that was read")
    template_ref: str = Field(description="The version the channel's file named; '' when no read has succeeded")
    read_at: float | None = Field(
        default=None, description="When that version was read, seconds since the epoch; null when none was"
    )
    checked_at: float | None = Field(
        default=None, description="When the channel's file was last asked for, whether or not it could be read"
    )
    error: str = Field(
        default="", description="Why the last read failed; '' when it succeeded. A failed read keeps the last version"
    )


class UpdateStateResponse(FrozenModel):
    """Everything a client reads about the workspace's updates. The files are answered as they are on disk."""

    settings: UpdateSettingsModel | None = Field(
        description="The update settings the workspace holds; null when no client has written any"
    )
    channel_version: UpdateChannelVersion | None = Field(
        description="The version the settings' channel is on, as the workspace last read it; null when it has read "
        "nothing for that channel. The workspace reads it at most once an hour, and this read does not"
    )
    schedule: dict[str, object] | None = Field(description="The armed schedule's file; null when none is armed")
    run: dict[str, object] | None = Field(description="The latest run's record; null when there is none")
    last_dispatch: dict[str, object] | None = Field(description="The record of the update chat last created")
    run_chat_id: str | None = Field(
        description="The id of the chat the run record names, looked up when the last dispatch does not name it: '' "
        "when the workspace lists no such chat, null when there was nothing to look up or the agents could not be read"
    )
    is_signed_in: bool | None = Field(
        description="Whether an AI account is signed in; null unless asked (check_sign_in=1) or the schedule's skip "
        "says none was"
    )
    skipped_target: dict[str, object] | None = Field(description="The skipped-version file; null when none")
    dismissals: dict[str, object] | None = Field(description="The dismissals file; null when none")
    last_start: UpdateStartAttempt | None = Field(description="The last start attempt; null when there was none")


class UpdateScheduleRefreshedResponse(FrozenModel):
    """What refreshing the schedule answers."""

    is_armed: bool = Field(description="Whether a schedule is armed; a refresh does nothing when none is")
    schedule: dict[str, object] | None = Field(description="The schedule file as the refresh left it")


class UpdateScheduleCancelledResponse(FrozenModel):
    """What cancelling the schedule answers."""

    is_cancelled: bool = Field(description="Whether there was a schedule to cancel")


class UpdateSkippedResponse(FrozenModel):
    """What skipping a version answers."""

    is_cancelled: bool = Field(description="Whether there was a schedule to cancel")
    skipped_target: dict[str, object] = Field(description="The skipped-version file as it now stands")


class UpdateDismissalsResponse(FrozenModel):
    """What recording a dismissal answers."""

    dismissals: dict[str, object] | None = Field(description="The dismissals file as it now stands; null when none")


class UpdateLivenessAgent(FrozenModel):
    """One of the workspace's agents, as the observer's event file lists it."""

    name: str = Field(description="The agent's name")
    state: str = Field(description="Its lifecycle state as mngr reports it; '' when the file does not say")
    agent_id: str = Field(description="Its id")
    chat_id: str = Field(description="The chat it takes messages for, when it is labelled with one; '' otherwise")


class UpdateBusyChat(FrozenModel):
    """One chat waiting on background tasks."""

    chat_id: str = Field(description="The chat")
    agent_id: str = Field(description="The chat's active agent's id; '' when unknown")
    agent_name: str = Field(description="The chat's active agent's name; '' when unknown")
    is_busy: bool = Field(description="Whether a task still pending will start the agent's next turn")


class UpdateLivenessResponse(FrozenModel):
    """What tells whether a running update is still alive, in one answer."""

    run: dict[str, object] | None = Field(description="The latest run's record; null when there is none")
    agents: tuple[UpdateLivenessAgent, ...] | None = Field(
        description="The workspace's agents; null when they could not be read, which a caller counts as alive"
    )
    busy_chats: tuple[UpdateBusyChat, ...] = Field(description="The chats waiting on background tasks")
    is_watchdog_installed: bool = Field(
        description="Whether the dispatcher's cron line is installed, so a stopped lead is brought back"
    )

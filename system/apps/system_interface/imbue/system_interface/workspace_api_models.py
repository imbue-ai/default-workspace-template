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
from workspace_update_control.state import UpdateAction
from workspace_update_control.state import UpdateActivity
from workspace_update_control.state import UpdateSkipReason
from workspace_update_control.state import UpdateStartFailureCode
from workspace_update_control.state import UpdateStartedBy
from workspace_update_control.state import UpdateVerdict

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
    """What the user put away: a run's note or outcome, named by the run's start as the read answers it
    (``run.started_at``, sent back as it was read), or a start that made no chat, named by its attempt."""

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
    start_failure_attempt_id: str | None = Field(
        default=None,
        min_length=1,
        description="The attempt of the failed start that was dismissed (start_failure.attempt_id); one that is "
        "no longer the last attempt records nothing",
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


class UpdateRunModel(FrozenModel):
    """The run a client shows: the one under way, or the last one for as long as its outcome or its note has not
    been dismissed."""

    started_at: float | None = Field(
        description="The run's start, seconds since the epoch, which is the name a dismissal gives the run: sent "
        "back exactly as read. For a chat that has recorded no run yet it is when the chat was made. Null while "
        "the chat is still being created, and for a record with no readable start, which nothing can dismiss"
    )
    chat_agent_name: str = Field(description="The name of the run's chat; '' while it is being created")
    chat_id: str = Field(description="That chat's id, for opening it; '' when it is unknown, or the chat is gone")
    is_chat_gone: bool = Field(description="Whether the run's chat was deleted, so there is no chat left to open")
    started_by: UpdateStartedBy | None = Field(
        description="Whether a schedule or a start now began the run; null when nothing recorded it"
    )
    target_ref: str = Field(
        description="The version the run aims at; '' for a run no start or schedule of the workspace's began"
    )
    override_ref: str = Field(description="The version the user named for the run; '' when it aims at the channel's")
    is_holding: bool = Field(
        description="Whether the run waits on a hold its lead recorded, for the user's go-ahead; false for a run "
        "that waits because its lead sits idle"
    )
    hold_detail: str = Field(description="One line naming what a recorded hold waits on; '' otherwise")
    verdict: UpdateVerdict | None = Field(
        description="How the run ended; null while it goes, once the user dismissed its outcome, and for a verdict "
        "this template does not know"
    )
    verdict_detail: str = Field(description="One line about the verdict; '' when verdict is null")
    in_place_compatible_ref: str = Field(
        description="On REFUSED or NEEDS_RECREATION, the newest version that could still be applied in place; '' "
        "when the run named none, or verdict is null"
    )
    updated_to: str = Field(
        description="The version a run that landed left the workspace on, until the user dismisses that note; '' "
        "otherwise. The note and the outcome are dismissed apart"
    )


class UpdateStartFailureModel(FrozenModel):
    """The last accepted start that made no chat."""

    attempt_id: str = Field(description="The attempt, which the start answered, and which a dismissal names")
    code: UpdateStartFailureCode = Field(description="Why no chat was made")
    detail: str = Field(description="What the create said, or '' when it said nothing")


class UpdateScheduleModel(FrozenModel):
    """The armed schedule."""

    target_ref: str = Field(description="The version the scheduled update goes to: the one named, else the ceiling")
    override_ref: str = Field(description="The version the user named; '' when the schedule follows the channel's")
    is_auto_armed: bool = Field(description="Whether auto-update armed it, rather than the user")
    window_opens_at: float | None = Field(
        description="When the window the update may start in opens, seconds since the epoch: the one open now "
        "(a moment in the past), else the next. Null for a schedule that holds no window, which starts nothing"
    )
    window_closes_at: float | None = Field(description="When that window closes; null with window_opens_at")
    last_skip_reason: UpdateSkipReason | None = Field(
        description="Why the last window closed without the update starting; null when it did not, and for a "
        "reason this template does not know"
    )


class UpdateStateResponse(FrozenModel):
    """Everything a client reads about the workspace's updates, as the workspace works it out when asked.

    What the update is doing (``activity``, ``run``, ``start_failure``, ``schedule``, ``needs_sign_in``) is the
    workspace's own reading of its files and its agents, so every client is told the same. Whether an update is
    on offer is left to the client, which compares ``installed_version`` with ``channel_version``.
    """

    as_of: float = Field(
        description="When the workspace worked this answer out, by its own clock, seconds since the epoch. Of two "
        "answers the later is the newer, so a client keeps the later and drops a read that a write overtook"
    )
    installed_version: str = Field(
        description="The version of the workspace's checkout, as the status route answers it; '' when it names none"
    )
    settings: UpdateSettingsModel | None = Field(
        description="The update settings the workspace holds; null when no client has written any"
    )
    channel_version: UpdateChannelVersion | None = Field(
        description="The version the settings' channel is on, as the workspace last read it; null when it has read "
        "nothing for that channel. The workspace reads it at most once an hour, and this read does not"
    )
    activity: UpdateActivity = Field(
        description="What an update run is doing. IDLE: none is in flight. STARTING: a start was accepted and its "
        "chat is being created. RUNNING: the chat exists and is preparing. WAITING: the run's lead waits on the "
        "user, on a hold it recorded or because it and its worker sit idle. APPLYING: the update is landing. "
        "FINISHING: it landed and the lead is finishing its checks. STALLED: the run has no verdict and nothing "
        "will carry it on, until its lead is back, it is dismissed, or an update starts"
    )
    run: UpdateRunModel | None = Field(
        description="The run under way, or the last run while its outcome or its note is not dismissed; null when "
        "there is nothing of a run to show"
    )
    start_failure: UpdateStartFailureModel | None = Field(
        description="The last accepted start when it made no chat; null once it is dismissed, a chat is made or a "
        "run begins, and for a start refused for want of a signed-in account, which needs_sign_in tells"
    )
    schedule: UpdateScheduleModel | None = Field(description="The armed schedule; null when none is armed")
    needs_sign_in: bool = Field(
        description="Whether a start, an arm or a scheduled start found no AI account signed in, and none is "
        "signed in now"
    )
    is_update_skipped: bool = Field(description="Whether the user skipped the version the channel is on")
    will_self_arm: bool = Field(
        description="Whether the workspace's dispatcher arms the update to the channel's version by itself within "
        "its next minute, so a client need not offer it"
    )
    is_backup_configured: bool = Field(description="Whether the workspace holds backup credentials")
    allowed_actions: tuple[UpdateAction, ...] = Field(
        description="What a client may ask as things stand. START and SCHEDULE: no run is in flight and none "
        "waits on a decision (SCHEDULE also needs cron). CANCEL_SCHEDULE: a schedule is armed. SKIP: the channel's "
        "version is read and not skipped. DISMISS_OUTCOME: a verdict, a stalled run or a failed start is showing. "
        "DISMISS_NOTE: run.updated_to is showing"
    )
    run_record: dict[str, object] | None = Field(
        description="The latest run's own record, as it is on disk; null when there is none. For a client that "
        "follows an apply, which reads when the apply last moved"
    )
    last_start: UpdateStartAttempt | None = Field(description="The last start attempt; null when there was none")


class UpdateOutcomeResponse(FrozenModel):
    """What starting an update or arming a schedule came to.

    ``outcome`` is one of ``STARTING``, ``ARMED``, ``ALREADY_RUNNING``, ``NEEDS_SIGN_IN``, ``NO_CRON`` and
    ``TOO_OLD_TO_UPDATE``; a later template may add one. A request that left its version or its window to the
    workspace may also be answered ``NO_CHANNEL_VERSION`` (the workspace has read no version for its release
    channel) or ``NO_UPDATE_SETTINGS`` (it holds no update settings).
    """

    outcome: str = Field(description="What the request came to")
    detail: str = Field(default="", description="One line about it")
    attempt_id: str = Field(
        default="", description="The start attempt a STARTING began (or one already under way); '' otherwise"
    )
    state: UpdateStateResponse = Field(description="The workspace's updates as the request left them")


class UpdateScheduleRefreshedResponse(FrozenModel):
    """What refreshing the schedule answers."""

    is_armed: bool = Field(description="Whether a schedule is armed; a refresh does nothing when none is")
    state: UpdateStateResponse = Field(description="The workspace's updates as the refresh left them")


class UpdateScheduleCancelledResponse(FrozenModel):
    """What cancelling the schedule answers."""

    is_cancelled: bool = Field(description="Whether there was a schedule to cancel")
    state: UpdateStateResponse = Field(description="The workspace's updates as the cancel left them")


class UpdateSkippedResponse(FrozenModel):
    """What skipping a version answers."""

    is_cancelled: bool = Field(description="Whether there was a schedule to cancel")
    state: UpdateStateResponse = Field(description="The workspace's updates as the skip left them")


class UpdateDismissedResponse(FrozenModel):
    """What recording a dismissal answers."""

    state: UpdateStateResponse = Field(description="The workspace's updates as the dismissal left them")


class UpdateSettingsWrittenResponse(FrozenModel):
    """What storing the update settings answers; the settings as stored are the state's."""

    state: UpdateStateResponse = Field(description="The workspace's updates as the write left them")

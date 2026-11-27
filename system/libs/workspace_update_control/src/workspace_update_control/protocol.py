from typing import Final

# The update state, relative to the workspace root, which the shell and cron's tick both run from.
UPDATE_STATE_DIR: Final[str] = "data/.state/update-apply"
RUN_RECORD_PATH: Final[str] = f"{UPDATE_STATE_DIR}/run.json"
SCHEDULE_FILE_PATH: Final[str] = f"{UPDATE_STATE_DIR}/schedule.json"
LAST_DISPATCH_FILE_PATH: Final[str] = f"{UPDATE_STATE_DIR}/last-dispatch.json"

# The per-workspace update facts that live beside the schedule: the release the user skipped for
# this workspace, and which runs' note and outcome they dismissed. Facts about one workspace every
# client should agree on, so they are kept in the workspace itself.
SKIPPED_TARGET_FILE_PATH: Final[str] = f"{UPDATE_STATE_DIR}/skipped-target.json"
DISMISSALS_FILE_PATH: Final[str] = f"{UPDATE_STATE_DIR}/dismissals.json"

# One attempt at starting an update now: who is creating the update's chat, and how it went. Only this
# library reads it; the dispatcher a desktop ships does not.
LAST_START_FILE_PATH: Final[str] = f"{UPDATE_STATE_DIR}/last-start.json"

# What a workspace that arms its own updates keeps, each in a file only this library writes. A desktop from
# before these files ships a program that reads none of them.
# The update settings a client wrote: the release channel, where its file is read from, whether updates are armed
# by the workspace, and the hours they may start in.
UPDATE_SETTINGS_FILE_PATH: Final[str] = f"{UPDATE_STATE_DIR}/settings.json"
# The channel's version as the workspace last read it, and how the last read went. The update-self skill reads
# this file for its default target, so its keys are that skill's too.
CHANNEL_VERSION_FILE_PATH: Final[str] = f"{UPDATE_STATE_DIR}/channel-version.json"
# The newest version a client last named in a request, and when: a desktop from before the workspace armed its own
# updates, which the dispatcher does not arm past while that desktop keeps asking.
CLIENT_CEILING_FILE_PATH: Final[str] = f"{UPDATE_STATE_DIR}/client-ceiling.json"
# Which values of the armed schedule the workspace filled in itself, and so keeps current.
SCHEDULE_ORIGIN_FILE_PATH: Final[str] = f"{UPDATE_STATE_DIR}/schedule-origin.json"
# The version of the checkout as the dispatcher last read it from git.
INSTALLED_VERSION_FILE_PATH: Final[str] = f"{UPDATE_STATE_DIR}/installed-version.json"
# When the dispatcher last tried to fetch the template's release tags into a checkout that named no version.
RELEASE_TAG_FETCH_FILE_PATH: Final[str] = f"{UPDATE_STATE_DIR}/release-tags-fetch.json"

# The user's timezone as the workspace records it (``workspace_profile.timezone`` writes it), which the update
# window's hours are read in.
USER_TIMEZONE_FILE_PATH: Final[str] = "data/.state/user_timezone"
# The backup credentials, whose presence is whether the workspace has backups to fall back on.
BACKUP_CREDENTIALS_FILE_PATH: Final[str] = "data/.secrets/restic.env"

# The cron drop-in that runs the dispatcher's tick, in the directory the cron daemon reads.
DISPATCHER_CRON_NAME: Final[str] = "update-self-dispatch"
LIVE_CRON_DIRECTORY: Final[str] = "/etc/cron.d"
# The cron line's durable copy, which the template reinstalls at boot.
DURABLE_CRON_PATH: Final[str] = f"data/.state/cron.d/{DISPATCHER_CRON_NAME}"

# How a run was started, as ``last-dispatch.json`` records it (the desktop's ``UpdateStartedBy``, lower-cased).
STARTED_BY_SCHEDULED: Final[str] = "scheduled"
STARTED_BY_IMMEDIATE: Final[str] = "immediate"

# What arming a schedule or starting an update came to (the desktop's ``UpdateArmOutcome``).
OUTCOME_ARMED: Final[str] = "ARMED"
OUTCOME_CREATED: Final[str] = "CREATED"
OUTCOME_ALREADY_RUNNING: Final[str] = "ALREADY_RUNNING"
OUTCOME_NEEDS_SIGN_IN: Final[str] = "NEEDS_SIGN_IN"
OUTCOME_CHAT_APP_UNREACHABLE: Final[str] = "CHAT_APP_UNREACHABLE"
OUTCOME_CREATE_FAILED: Final[str] = "CREATE_FAILED"
OUTCOME_NO_CRON: Final[str] = "NO_CRON"
OUTCOME_TOO_OLD_TO_UPDATE: Final[str] = "TOO_OLD_TO_UPDATE"
# A start that passed its checks: the update's chat is being created, and ``last-start.json`` says how it went.
OUTCOME_STARTING: Final[str] = "STARTING"
# A request named no version and the workspace has read none for its release channel.
OUTCOME_NO_CHANNEL_VERSION: Final[str] = "NO_CHANNEL_VERSION"
# A request named no update window and the workspace holds no update settings to take one from.
OUTCOME_NO_UPDATE_SETTINGS: Final[str] = "NO_UPDATE_SETTINGS"
# The workspace's own arm found a schedule armed already and left it as it stands. No route answers this: an
# arm a client asks for replaces whatever is armed.
OUTCOME_ALREADY_ARMED: Final[str] = "ALREADY_ARMED"

# Why a window closed without its scheduled run starting (the desktop's ``UpdateSkipReason``).
SKIP_UPDATE_IN_FLIGHT: Final[str] = "UPDATE_IN_FLIGHT"
SKIP_LEASE_HELD: Final[str] = "LEASE_HELD"
SKIP_NEEDS_SIGN_IN: Final[str] = "NEEDS_SIGN_IN"
SKIP_DISPATCH_FAILED: Final[str] = "DISPATCH_FAILED"
SKIP_WINDOW_CLOSED: Final[str] = "WINDOW_CLOSED"

# The update chat's first message, whether the run was scheduled or pressed: the lead is told the target
# and the app's ceiling and never phones home for them. The skill records its own chat name in
# ``run.json``, which is what the app's probes read, so a hand-launched run is identified the same way.
UPDATE_COMMAND: Final[str] = "/update-self"

# Read by the skill's Step 2 from the release that ships the pinned-target prose on.
PINNED_NOTE_TEMPLATE: Final[str] = (
    "The Imbue Studio app pinned this update, so do not ask it for its version:\n"
    "- Target: {target}\n"
    "- Ceiling: {ceiling}"
)

# CLEANUP: drop this instruction once the oldest release the app updates in place
# (``OLDEST_IN_PLACE_UPDATABLE_VERSION``) ships update-self prose whose Step 1 commits a dirty tree
# itself: until then a workspace's local Step 1 refuses one, and nobody may be there to clean it.
OLD_STEP_ONE_INSTRUCTION: Final[str] = (
    "If Step 1 finds uncommitted changes, do not stop the update for them: commit them all as one "
    "work-in-progress commit on the current branch, with a message that lists their paths, name that "
    "commit in your results, and carry on."
)

# CLEANUP: drop this instruction once the oldest release the app updates in place
# (``OLDEST_IN_PLACE_UPDATABLE_VERSION``) ships update-self prose whose Step 2 reads the pinned
# lines above itself: until then a workspace's local Step 2 predates them, and ``--ceiling`` is
# the flag every release back to minds-v0.3.10 takes.
OLD_STEP_TWO_INSTRUCTION_TEMPLATE: Final[str] = (
    "When you resolve the target in Step 2, append `--ceiling {ceiling}` to the `resolve-target` command. "
    "If the app cannot be reached for anything else during this run, that is not a fault."
)

# Names the target and that the user already confirmed it, but no flag or step: the skill
# re-points itself at the target version's own copy of its flow, so the mechanism may differ.
OVERRIDE_NOTE_TEMPLATE: Final[str] = (
    "The user chose a specific version in the Imbue Studio app: update this workspace to {target}. "
    "This is their explicit override, chosen knowing it may be newer than the app or not a "
    "release. Treat it as their confirmation of the target; do not ask them to confirm it again."
)

# CLEANUP: drop this note (and the ``is_backup_configured`` field that selects it) once no
# workspace can still be running an update-self whose preconditions stop to ask about a missing
# restore point -- i.e. once the oldest template an update can be applied to is past the release
# that made the flow unattended.
NO_BACKUP_NOTE: Final[str] = (
    "This workspace has no backups configured, so this update has no restore point to fall back "
    "on. The Imbue Studio app said so before the user started it, and they chose to go ahead without "
    "one. Treat that as their go-ahead; do not stop to ask them about the missing backup."
)

# What the watchdog sends an idle lead whose run has no verdict.
WATCHDOG_REMINDER_MESSAGE: Final[str] = (
    "A note from the Imbue Studio app: this update has no recorded result yet, and neither you nor "
    "its background agent is working on it. Carry on if you can. If the update has ended, record "
    "the verdict the update-self skill calls for, and tell the user; if a fault stopped it and you "
    'cannot recover, that is `run-status verdict STUCK --detail "<why, one plain line>"` from the '
    "update-self script. Until a result is recorded you will keep being reminded."
)

# What the watchdog sends a stopped lead whose run has no verdict; sending it is what restarts the chat.
WATCHDOG_REVIVE_MESSAGE_TEMPLATE: Final[str] = (
    "A note from the Imbue Studio app: this update's chat stopped before the update recorded a result "
    "(the workspace may have restarted), so the app has started it again. Restarts so far: {count}. "
    "Check where the update got to and carry on. If it keeps stopping for a reason you cannot find or "
    'fix, record `run-status verdict STUCK --detail "<why, one plain line>"` from the update-self '
    "script and tell the user."
)

# The verdict detail the watchdog records for a run whose lead's chat was deleted.
WATCHDOG_GONE_LEAD_DETAIL: Final[str] = (
    "The update's chat was deleted before the update finished."
)

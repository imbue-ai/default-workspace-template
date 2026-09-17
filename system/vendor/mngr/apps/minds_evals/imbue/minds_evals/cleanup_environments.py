"""Delete the Modal environments an eval run left behind.

A trial never destroys its own environment: it is where a person debugging that trial recovers the
workspace's state from, long after the sandboxes inside it are gone. So the environments accumulate
on purpose, and something outside the trial has to remove them.

There are two scopes. Naming a job directory deletes exactly the environments that job's own trials
recorded and nothing else, which is what a developer runs against their own run. Sweeping by prefix
and age is the backstop for a scheduled run whose job directory did not survive.

Two kinds of guard constrain the sweep, and they do different jobs. The request is bounded first,
from the request alone: a prefix carrying no `ci-` marker is refused, as is an age that would put the
cutoff at or after now and so select the environments a batch running right now just created. What
decides that a matching name belongs to a *finished* scheduled run is the embedded `ci-<timestamp>`:
a prefix match on its own proves nothing, because the trial name follows the eval namespace directly,
so a developer's run of a case whose id begins with `ci-` lands under the same prefix. Such a name
carries no parseable stamp, so it is skipped -- which is why the timestamp check is the fence and not
a convenience, and must not be simplified away.

Deleting an environment needs manage access to the Modal workspace. The CI token has it; a
developer's token may not, in which case the deletion reports as failed rather than raising.
"""

import re
from abc import ABC
from abc import abstractmethod
from collections.abc import Sequence
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Final
from typing import assert_never

import modal.environments
from loguru import logger
from modal.exception import Error as ModalError
from modal.exception import NotFoundError as ModalNotFoundError

from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.minds_evals.check_run import list_trial_dirs
from imbue.minds_evals.check_run import read_trial_state
from imbue.minds_evals.data_types import CleanupReport
from imbue.minds_evals.data_types import ModalDeletionOutcome
from imbue.minds_evals.errors import CleanupScopeError
from imbue.minds_evals.errors import JobReadError
from imbue.minds_evals.errors import ModalAdminError

# The marker a scheduled run stamps into every trial's Modal user id, and which a sweep prefix must
# carry. This bounds what an operator can point the sweep at; it does not by itself identify a
# scheduled run's environment, because a case id may start with `ci-` too -- the timestamp below is
# what settles that.
CI_NAME_MARKER: Final[str] = "ci-"
# The timestamp a scheduled run embeds after that marker, in the same compact UTC shape the minds
# deployment CI uses (`ci-YYYYMMDDtHHMMSSz`). Lexical order equals chronological order.
_CI_TIMESTAMP_FORMAT: Final[str] = "%Y%m%dt%H%M%Sz"
_CI_TIMESTAMP_PATTERN: Final[re.Pattern[str]] = re.compile(r"ci-(\d{8}t\d{6}z)")


@pure
def format_ci_user_id_prefix(minted_at: datetime) -> str:
    """The `--ak user_id_prefix=` value a scheduled run passes, given the moment it started."""
    return "{}{}-".format(CI_NAME_MARKER, minted_at.astimezone(timezone.utc).strftime(_CI_TIMESTAMP_FORMAT))


@pure
def parse_ci_timestamp(environment_name: str) -> datetime | None:
    """The moment embedded in a scheduled run's environment name, or None when it carries none."""
    match = _CI_TIMESTAMP_PATTERN.search(environment_name)
    if match is None:
        return None
    try:
        return datetime.strptime(match.group(1), _CI_TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


@pure
def check_sweep_prefix_is_scoped(sweep_prefix: str) -> None:
    """Raises CleanupScopeError for a prefix too broad to be a scheduled run's at all.

    Decided from the request alone, so a caller can refuse an over-broad sweep before it reaches
    Modal for anything.
    """
    if CI_NAME_MARKER not in sweep_prefix:
        raise CleanupScopeError(
            "sweep prefix {!r} does not contain {!r}, so it could match a developer's environment; "
            "sweep only prefixes a scheduled run stamps".format(sweep_prefix, CI_NAME_MARKER)
        )


@pure
def check_sweep_age_is_bounded(older_than_hours: float) -> None:
    """Raises CleanupScopeError for an age that would put the cutoff at or after now.

    The cutoff is `now - age`, so an age of zero or less selects every stamped name under the
    prefix, the environments a batch running right now just created among them. Decided from the
    request alone, beside the prefix check, so an inadmissible sweep is refused before Modal is
    reached for anything.
    """
    if older_than_hours <= 0:
        raise CleanupScopeError(
            "a sweep age must be greater than 0 hours; {} would put the cutoff at or after now and "
            "take the environments a batch running right now just created".format(older_than_hours)
        )


def select_sweepable_environment_names(
    environment_names: Sequence[str],
    sweep_prefix: str,
    cutoff: datetime,
) -> tuple[str, ...]:
    """The environments a prefix-and-age sweep may delete: stamped by a scheduled run, and old enough.

    Not `@pure`: a name under the prefix that cannot be aged is skipped, and the skip is warned
    about rather than left silent.

    Raises CleanupScopeError for a prefix too broad to be a scheduled run's at all.
    """
    check_sweep_prefix_is_scoped(sweep_prefix)
    selected: list[str] = []
    for environment_name in environment_names:
        if not environment_name.startswith(sweep_prefix):
            continue
        minted_at = parse_ci_timestamp(environment_name)
        # This is the guard that keeps a developer's environment out, not the prefix: a case id
        # beginning with `ci-` puts a developer run under the same prefix, and such a name carries no
        # stamp. It also gives the sweep the age it needs, so a run still going is never taken.
        if minted_at is None:
            logger.warning("Skipping {}: it carries no parseable ci- timestamp to age it by", environment_name)
            continue
        if minted_at < cutoff:
            selected.append(environment_name)
    return tuple(sorted(selected))


def read_job_environment_names(job_dir: Path) -> tuple[str, ...]:
    """The Modal environments one job's own trials recorded, and only those.

    Deliberately more forgiving than the run gate that reads the same directory. The gate refuses a
    job whose artifacts cannot be parsed, because an unjudged run must never read as a pass; cleanup
    asks "what did this job record?", and a job with no trials, or with one trial whose state was
    truncated by the crash being cleaned up after, still has an answer for the rest. Inheriting the
    gate's strictness would mean one unreadable trial leaks every environment of the job -- and the
    age-based sweep is no backstop for the run that just made them, since its cutoff is hours older
    than the run's own budget.
    """
    if not job_dir.is_dir():
        raise JobReadError("{} is not a job directory".format(job_dir))
    environment_names: set[str] = set()
    for trial_dir in list_trial_dirs(job_dir):
        try:
            state = read_trial_state(trial_dir)
        except JobReadError as exc:
            logger.warning("Skipping {}: its state cannot be read ({})", trial_dir.name, exc)
            continue
        environment_name = str((state or {}).get("modal_environment_name") or "")
        if environment_name:
            environment_names.add(environment_name)
    if not environment_names:
        logger.warning("{} recorded no Modal environments -- nothing named to clean up", job_dir)
    return tuple(sorted(environment_names))


class ModalEnvironmentAdminInterface(MutableModel, ABC):
    """Lists and deletes Modal environments in the workspace the local Modal credentials select."""

    @abstractmethod
    def list_environment_names(self) -> tuple[str, ...]:
        """Every Modal environment name the current Modal credentials can see."""

    @abstractmethod
    def delete_environment(self, environment_name: str) -> ModalDeletionOutcome:
        """Delete one environment along with the apps and volumes inside it."""


class ModalSdkEnvironmentAdmin(ModalEnvironmentAdminInterface):
    """Drives the Modal SDK that harbor's modal extra installs into this project's own venv.

    The credentials are whatever the SDK resolves (`~/.modal.toml`'s active profile, or the
    `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET` pair), which is the same workspace the trials created
    their environments in.
    """

    def list_environment_names(self) -> tuple[str, ...]:
        try:
            entries = modal.environments.list_environments()
        except ModalError as exc:
            raise ModalAdminError("could not list Modal environments: {}".format(exc)) from exc
        return tuple(str(entry.name) for entry in entries if entry.name)

    def delete_environment(self, environment_name: str) -> ModalDeletionOutcome:
        try:
            modal.environments.delete_environment(environment_name)
        except ModalNotFoundError:
            return ModalDeletionOutcome.NOT_FOUND
        except ModalError as exc:
            # Permission denied (a token without manage access to the workspace) lands here too:
            # reported, so the caller sees the environment is still there, never raised, so one
            # refusal does not stop the rest of the pass.
            logger.warning("Could not delete {}: {}", environment_name, str(exc)[:300])
            return ModalDeletionOutcome.FAILED
        return ModalDeletionOutcome.DELETED


def delete_environments(
    admin: ModalEnvironmentAdminInterface,
    environment_names: Sequence[str],
) -> CleanupReport:
    """Delete each named environment, tolerating one that is already gone."""
    deleted_names: list[str] = []
    already_gone_names: list[str] = []
    failed_names: list[str] = []
    for environment_name in environment_names:
        outcome = admin.delete_environment(environment_name)
        match outcome:
            case ModalDeletionOutcome.DELETED:
                logger.info("Deleted {}", environment_name)
                deleted_names.append(environment_name)
            case ModalDeletionOutcome.NOT_FOUND:
                logger.info("Already gone: {}", environment_name)
                already_gone_names.append(environment_name)
            case ModalDeletionOutcome.FAILED:
                failed_names.append(environment_name)
            case _ as unreachable:
                assert_never(unreachable)
    return CleanupReport(
        deleted_names=tuple(deleted_names),
        already_gone_names=tuple(already_gone_names),
        failed_names=tuple(failed_names),
    )


@pure
def resolve_sweep_cutoff(older_than_hours: float, now: datetime) -> datetime:
    """Environments minted before this are old enough for a sweep to remove."""
    return now - timedelta(hours=older_than_hours)


def select_swept_environment_names(
    admin: ModalEnvironmentAdminInterface,
    sweep_prefix: str,
    older_than_hours: float,
    now: datetime,
) -> tuple[str, ...]:
    """The environments the backstop sweep would take: everything the workspace holds, narrowed to a
    scheduled run's stamped names under the prefix and old enough to be finished.

    Raises CleanupScopeError for a request too broad to be a scheduled run's, by prefix or by age.
    """
    # Before the listing, not after it: an over-broad request is inadmissible whatever the workspace
    # holds, and a guard that only fires past a Modal round trip cannot be reached without Modal
    # credentials at all.
    check_sweep_prefix_is_scoped(sweep_prefix)
    check_sweep_age_is_bounded(older_than_hours)
    return select_sweepable_environment_names(
        admin.list_environment_names(), sweep_prefix, resolve_sweep_cutoff(older_than_hours, now)
    )


def run_cleanup(
    admin: ModalEnvironmentAdminInterface,
    environment_names: Sequence[str],
    is_dry_run: bool,
) -> CleanupReport | None:
    """Delete the named environments, or report what a dry run would have deleted.

    Returns None when nothing was attempted -- an empty selection, or a dry run -- which is what
    separates "the pass deleted nothing" from "the pass had nothing to delete".
    """
    if not environment_names:
        logger.info("Nothing to delete")
        return None
    if is_dry_run:
        logger.info("Would delete {} environment(s): {}", len(environment_names), ", ".join(environment_names))
        return None
    report = delete_environments(admin, environment_names)
    logger.info(
        "Deleted {}, already gone {}, failed {}",
        len(report.deleted_names),
        len(report.already_gone_names),
        len(report.failed_names),
    )
    return report

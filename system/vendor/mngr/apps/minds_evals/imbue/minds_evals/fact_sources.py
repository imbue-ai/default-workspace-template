"""Read one finished trial's job-directory files into the typed inputs the facts are computed from.

`evidence_facts.py` states what a trial recorded; this module is the only place that opens a file to
find out. Every reading is per step, over the layout `step_artifacts.py` resolves, and a record the
step does not hold reads as None or as an empty value. Which of the three outcomes that is -- the
record was never due, or was due and never written -- is the fact functions' to decide, since the
case declaration they read is what says what the step owed.

Nothing here decides whether a reading is good. A parse that fails yields None rather than raising,
because a record the checker could not read is a fact about the instrument, not an error in the
checker -- the exception is a file harbor itself wrote (`result.json`), whose unreadability is a job
that cannot be read at all and stops the check.
"""

import json
import tarfile
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from typing import Final

from harbor.models.trial.paths import TrialPaths
from harbor.models.trial.result import TrialResult
from loguru import logger
from pydantic import Field
from pydantic import ValidationError

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessError
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.minds_evals import evidence_collection
from imbue.minds_evals.check_run import describe_incompletion
from imbue.minds_evals.check_run import load_trial_result
from imbue.minds_evals.data_types import CaseConfig
from imbue.minds_evals.data_types import EvidenceManifest
from imbue.minds_evals.data_types import RegisteredApp
from imbue.minds_evals.data_types import TicketRecord
from imbue.minds_evals.data_types import WorkerListingEntry
from imbue.minds_evals.driver import DRIVER_EVENTS_FILENAME
from imbue.minds_evals.driver import DRIVER_LOG_FILENAME
from imbue.minds_evals.driver import INSTRUCTION_FILENAME
from imbue.minds_evals.driver import TRAJECTORY_FILENAME
from imbue.minds_evals.driver import parse_case_config
from imbue.minds_evals.errors import InstructionParseError
from imbue.minds_evals.step_artifacts import StepArtifactPaths
from imbue.minds_evals.step_artifacts import resolve_step_artifact_paths

# Where the driver keeps the workspace snapshots it pulled, under the step's agent dir.
SNAPSHOTS_DIRNAME: Final[str] = "snapshots"
SNAPSHOT_SUFFIX: Final[str] = ".tar.gz"

# What a flow's frames are named; every other name in a flow directory is a record the loader reads.
FLOW_FRAME_SUFFIX: Final[str] = ".png"

# The verifier's derived outputs, as templates/tests/verifier/keep_derived_outputs.py names them.
JUDGE_FLOWS_DIGEST_FILENAME: Final[str] = "judge_flows_digest.txt"
JUDGE_SCREENSHOT_NAMES_FILENAME: Final[str] = "judge_screenshots.txt"
JUDGE_TRANSCRIPT_FILENAME: Final[str] = "judge_transcript.txt"
PROGRESS_SUMMARY_FILENAME: Final[str] = "progress_summary.json"
HARNESS_FAILURES_FILENAME: Final[str] = "harness_failures.json"

# The first field of a driver.log line, which loguru writes as `<time> | <level> | ...`.
_DRIVER_LOG_FIELD_SEPARATOR: Final[str] = "|"

# How long a git call about one captured bundle may take before the reading is given up on.
_GIT_BUNDLE_TIMEOUT_SECONDS: Final[float] = 60.0
# What `git bundle verify` says about an incremental bundle whose base commit the check-time repo
# does not hold. That is every captured bundle: the base is the eval-case commit, which exists only
# in the workspace the trial destroyed. Such a bundle is well-formed and only its prerequisite is
# absent, so that one report reads as verified; a header that is not a bundle's does not.
_MISSING_PREREQUISITES_MESSAGE: Final[str] = "Repository lacks these prerequisite commits"


class ReadFile(FrozenModel):
    """One file the checker opened: whether it was there, and what it held.

    Presence and contents answer separately, so an absent file is never read as an empty one -- the
    difference between a record that was never written and one that says nothing.
    """

    is_present: bool = Field(description="Whether the file exists at all")
    text: str = Field(description="Its decoded contents; empty when it is absent or could not be decoded")
    is_readable: bool = Field(description="Whether it exists and decoded")


class CapturedWorkerRecord(FrozenModel):
    """One worker as `verification/workers/captures.json` records it.

    An overflowed launch is a name and nothing else: the caps stopped the capture before it looked,
    so every field a capture would have answered is left at its empty value rather than at a reading.
    """

    name: str = Field(description="The worker's mngr agent name")
    agent_type: str = Field(description="The worker's agent type; empty when neither listing nor document said")
    state: str = Field(description="The worker's lifecycle state at collection time; empty on an overflow")
    is_stream_captured: bool = Field(description="Whether the worker's own transcript stream came out")
    is_report_captured: bool = Field(description="Whether the worker's finish report came out")


class WorkerListingStatus(FrozenModel):
    """What `verification/workers/listing.json` says about the listing command itself."""

    exit_code: int | None = Field(description="The listing command's exit code; None when it records none")
    errors: tuple[str, ...] = Field(description="What the listing could not reach")
    is_complete: bool = Field(description="Whether the listing reported every agent without error")


class FlowSources(FrozenModel):
    """What one UI flow's evidence directory holds."""

    is_present: bool = Field(description="Whether the directory holds a log or a run record")
    records: tuple[dict[str, Any], ...] = Field(description="The flow's log.jsonl records, in order")
    frame_count: int = Field(description="How many .png frames the directory holds")
    run: dict[str, Any] | None = Field(description="The closing run record; None when the flow wrote none")


class StepFactSources(FrozenModel):
    """Everything one step of one trial recorded, as the files of its step directory hold it."""

    step_name: str = Field(description="The step's name; empty for a flat trial's one step")
    step_index: int = Field(description="The step's 0-based position among the steps harbor ran")
    step_names_so_far: tuple[str, ...] = Field(
        description="The steps harbor ran up to and including this one, in order; empty for a flat trial"
    )
    incompletion_reason: str = Field(
        description="Why the trial did not run to the end as of this step, by check-run's criterion; empty when it did"
    )
    spend_input_token_deltas: tuple[int | None, ...] = Field(
        description="What harbor recorded as each step's published input-token delta, up to this step"
    )
    previous_driver_log_last_timestamp: str = Field(
        description="The last timestamp the previous step's driver log carries; empty when there is no previous step"
    )

    state: dict[str, Any] | None = Field(description="The driver's state.json; None when absent or unreadable")
    case: CaseConfig | None = Field(
        description="The case config instruction.md carries; None when it is absent or unparsable"
    )
    trajectory: dict[str, Any] | None = Field(
        description="The published ATIF document; None when absent or unreadable"
    )
    is_driver_events_present: bool = Field(description="Whether the driver's own event record exists at all")
    driver_events: tuple[dict[str, Any], ...] = Field(
        description="driver_events.jsonl's records, in order; each names its DriverEventType under `type`, and a "
        "feed event keeps the workspace's own event whole under `event`"
    )
    driver_log: ReadFile = Field(description="The driver's own log for this step")
    step_metadata: dict[str, Any] | None = Field(
        description="What harbor's result.json records as this step's agent metadata; None when it records none"
    )
    snapshot_path: Path | None = Field(
        description="The newest workspace snapshot the step kept; None when it kept none"
    )

    manifest: EvidenceManifest | None = Field(description="The evidence manifest; None when absent or unreadable")
    manifest_block: dict[str, Any] | None = Field(
        description="That same manifest as the raw JSON object, for keys this package's model does not yet name"
    )
    manifest_path: Path = Field(description="Where the manifest is, for the readers that name it in a warning")
    apps_registry: ReadFile = Field(description="The workspace app registry capture (apps.toml)")
    services: ReadFile = Field(description="The supervisorctl status capture")
    supervisord_conf: ReadFile = Field(description="The workspace supervisord config capture")
    isolated_instance_services: frozenset[str] = Field(description="Registry rows owned by throwaway preview servers")
    repo_state: dict[str, Any] | None = Field(description="repo_state.json; None when absent or unreadable")
    bundle_path: Path | None = Field(description="The captured deliverable bundle; None when none was captured")
    tickets: tuple[TicketRecord, ...] = Field(description="The captured tickets, in file order")
    tickets_failure_reason: str = Field(description="Why the tickets capture failed; empty when it did not")
    is_tickets_present: bool = Field(description="Whether tickets.jsonl exists at all")
    agent_inventory: tuple[WorkerListingEntry, ...] = Field(description="Every agent the workspace listing named")
    is_agent_listing_present: bool = Field(description="Whether workers/agents.json exists at all")
    worker_listing_status: WorkerListingStatus | None = Field(
        description="What the listing command reported; None when workers/listing.json is absent or unreadable"
    )
    worker_captures: tuple[CapturedWorkerRecord, ...] = Field(description="One record per worker the step captured")
    is_worker_captures_present: bool = Field(description="Whether workers/captures.json exists at all")
    flows_by_slug: dict[str, FlowSources] = Field(description="Each flow's evidence, by its slugified name")

    reward_details: dict[str, Any] | None = Field(description="The step's rewardkit breakdown; None when absent")
    judge_flows_digest: ReadFile = Field(description="The flow digest the outcome judge read")
    judge_screenshots: ReadFile = Field(description="The screenshot listing the outcome judge read")
    judge_transcript: ReadFile = Field(description="The transcript the outcome judge read, progress blocks included")
    progress_summary: ReadFile = Field(description="The verifier's count of the progress blocks it rendered")
    harness_failures: ReadFile = Field(description="The verifier's failure-signature counts")
    diagnostic_probe: ReadFile = Field(description="The self-diagnostic probe's raw output, on a step that runs it")


class CheckTimeReadings(FrozenModel):
    """The readings the checker had to run a program or open an archive for, taken once per step.

    They are kept apart from the parsed files so that every fact function stays pure: a tarball
    listing and a `git bundle verify` are the only two things a fact needs that reading a file
    cannot give.
    """

    snapshot_member_names: tuple[str, ...] | None = Field(
        description="Every path the step's last snapshot lists; None when there is none or it cannot be listed"
    )
    bundle_byte_count: int | None = Field(description="The captured bundle's size; None when none was captured")
    is_bundle_verified: bool = Field(description="Whether `git bundle verify` accepts the captured bundle")


class TrialFactSources(FrozenModel):
    """One trial's steps, in the order harbor ran them."""

    trial_name: str = Field(description="The trial directory's name")
    case_id: str = Field(description="The case the trial's last written state names; empty when none says")
    harness: str = Field(description="The harness the trial's arm records; empty when it records none")
    incompletion_reason: str = Field(description="Why the trial did not run to the end; empty when it did")
    steps: tuple[StepFactSources, ...] = Field(description="One entry per step harbor ran, in order")


def _read_file(path: Path) -> ReadFile:
    """The file's decoded contents, with absence and an undecodable file told apart."""
    if not path.is_file():
        return ReadFile(is_present=False, text="", is_readable=False)
    try:
        return ReadFile(is_present=True, text=path.read_bytes().decode(), is_readable=True)
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning("Could not read {}: {}", path, exc)
        return ReadFile(is_present=True, text="", is_readable=False)


def _read_json_object(path: Path) -> dict[str, Any] | None:
    """The file as a JSON object, or None when it is absent, undecodable, or not an object."""
    read = _read_file(path)
    if not read.is_readable:
        return None
    try:
        parsed = json.loads(read.text)
    except ValueError as exc:
        logger.warning("{} is not valid JSON: {}", path, exc)
        return None
    if not isinstance(parsed, dict):
        logger.warning("{} is a {}, not a JSON object", path, type(parsed).__name__)
        return None
    return parsed


def _read_json_array(path: Path) -> tuple[list[Any] | None, bool]:
    """The file as a JSON array and whether it is there at all; None for one that is neither."""
    read = _read_file(path)
    if not read.is_readable:
        return None, read.is_present
    try:
        parsed = json.loads(read.text)
    except ValueError as exc:
        logger.warning("{} is not valid JSON: {}", path, exc)
        return None, True
    return (parsed, True) if isinstance(parsed, list) else (None, True)


@pure
def parse_jsonl_objects(text: str) -> tuple[dict[str, Any], ...]:
    """Every line of a JSONL capture that is a JSON object, in order. A line that is neither is
    dropped: these files are appended to by a writer that can be killed mid-line."""
    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return tuple(records)


@pure
def _parse_worker_listing_status(block: Mapping[str, Any] | None) -> WorkerListingStatus | None:
    if block is None:
        return None
    raw_errors = block.get("errors")
    exit_code = block.get("exit_code")
    return WorkerListingStatus(
        exit_code=exit_code if isinstance(exit_code, int) and not isinstance(exit_code, bool) else None,
        errors=tuple(str(error) for error in raw_errors) if isinstance(raw_errors, list) else (),
        is_complete=block.get("is_complete") is True,
    )


@pure
def parse_worker_captures(records: Sequence[Any]) -> tuple[CapturedWorkerRecord, ...]:
    """The capture records the collector wrote, in the shape `worker_captures_json` writes them."""
    return tuple(
        CapturedWorkerRecord(
            name=str(record.get("name") or ""),
            agent_type=str(record.get("agent_type") or ""),
            state=str(record.get("state") or ""),
            is_stream_captured=record.get("is_stream_captured") is True,
            is_report_captured=record.get("is_report_captured") is True,
        )
        for record in records
        if isinstance(record, Mapping) and record.get("name")
    )


def _read_manifest(path: Path) -> tuple[EvidenceManifest | None, dict[str, Any] | None]:
    """The manifest as its model, and as the raw object beside it.

    Both shapes travel: the model is what the facts read, and the raw object carries the keys a
    later collector writes before this package's model names them.
    """
    block = _read_json_object(path)
    if block is None:
        return None, None
    try:
        return EvidenceManifest.model_validate(block), block
    except ValidationError as exc:
        logger.warning("{} is not an evidence manifest: {}", path, exc)
        return None, block


@pure
def _ticket_record_fields(block: Mapping[str, Any]) -> dict[str, Any]:
    """One captured ticket under this package's own field names. The capture writes the ticket file's
    key names (`id`, `type`), which the model carries as serialization aliases."""
    renamed = {"id": "ticket_id", "type": "ticket_type"}
    return {renamed.get(key, key): value for key, value in block.items()}


def _read_tickets(path: Path) -> tuple[tuple[TicketRecord, ...], str, bool]:
    """The captured tickets, the capture's failure reason, and whether the file is there at all.

    A failed capture writes one record carrying `failure_reason` instead of the tickets, so both are
    read from the same file and a capture that never ran is neither.
    """
    read = _read_file(path)
    if not read.is_readable:
        return (), "", read.is_present
    records: list[TicketRecord] = []
    for block in parse_jsonl_objects(read.text):
        failure_reason = str(block.get("failure_reason") or "")
        if failure_reason:
            return (), failure_reason, True
        try:
            records.append(TicketRecord.model_validate(_ticket_record_fields(block)))
        except ValidationError as exc:
            logger.warning("Dropping a ticket record of {}: {}", path, exc)
    return tuple(records), "", True


def _read_flow(flow_dir: Path) -> FlowSources:
    """One flow's evidence: its log records, its frames, and the run record that closes it."""
    log_read = _read_file(flow_dir / evidence_collection.FLOW_LOG_FILENAME)
    run = _read_json_object(flow_dir / evidence_collection.FLOW_RUN_FILENAME)
    return FlowSources(
        is_present=log_read.is_present or run is not None,
        records=parse_jsonl_objects(log_read.text),
        frame_count=sum(
            1 for entry in flow_dir.iterdir() if entry.is_file() and entry.name.endswith(FLOW_FRAME_SUFFIX)
        ),
        run=run,
    )


def _read_flows(flows_dir: Path) -> dict[str, FlowSources]:
    if not flows_dir.is_dir():
        return {}
    return {entry.name: _read_flow(entry) for entry in sorted(flows_dir.iterdir()) if entry.is_dir()}


@pure
def _snapshot_order(snapshot_path: Path) -> tuple[int, str]:
    """What orders one snapshot against another: the turn number the driver stamped on it.

    The stamp is not zero-padded (`post_message_10`), so name order is not pull order, and the last
    tarball is the one the state's byte count describes.
    """
    _head, _separator, stamp = snapshot_path.name.removesuffix(SNAPSHOT_SUFFIX).rpartition("_")
    return (int(stamp) if stamp.isdigit() else -1, snapshot_path.name)


def _newest_snapshot_path(snapshots_dir: Path) -> Path | None:
    """The last snapshot the step pulled."""
    if not snapshots_dir.is_dir():
        return None
    snapshots = sorted(
        (entry for entry in snapshots_dir.iterdir() if entry.is_file() and entry.name.endswith(SNAPSHOT_SUFFIX)),
        key=_snapshot_order,
    )
    return snapshots[-1] if snapshots else None


def _read_case_config(instruction_path: Path) -> CaseConfig | None:
    """The case the step drove, read with the driver's own instruction parser; None when the
    instruction is absent or carries no config this can read."""
    read = _read_file(instruction_path)
    if not read.is_readable:
        return None
    try:
        return parse_case_config(read.text)
    except (InstructionParseError, ValidationError) as exc:
        logger.warning("Could not read the case config out of {}: {}", instruction_path, exc)
        return None


def is_bundle_verified(bundle_path: Path, work_dir: Path) -> bool:
    """Whether `git bundle verify` accepts the captured bundle, run in a repo of its own.

    A captured bundle is incremental against the eval-case commit, which no check-time repo can hold,
    so verify reports that one prerequisite as missing on a perfectly good bundle; see
    `_MISSING_PREREQUISITES_MESSAGE`.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    # `git -C` moves git into the work dir, so a bundle named relative to the caller's own directory
    # -- which is how a job directory is usually given -- would be looked for in the wrong place.
    bundle_argument = str(bundle_path.resolve())
    try:
        with ConcurrencyGroup(name="minds-evals-bundle-verify") as group:
            initialized = group.run_process_to_completion(
                ["git", "init", "--quiet", str(work_dir)],
                is_checked_after=False,
                timeout=_GIT_BUNDLE_TIMEOUT_SECONDS,
            )
            if initialized.returncode != 0:
                logger.warning("Could not make a repo to verify {} in: {}", bundle_path, initialized.stderr.strip())
                return False
            verified = group.run_process_to_completion(
                ["git", "-C", str(work_dir), "bundle", "verify", bundle_argument],
                is_checked_after=False,
                timeout=_GIT_BUNDLE_TIMEOUT_SECONDS,
            )
    except (OSError, ProcessError) as exc:
        logger.warning("Could not verify {}: {}", bundle_path, exc)
        return False
    return verified.returncode == 0 or _MISSING_PREREQUISITES_MESSAGE in verified.stderr


def snapshot_member_names(snapshot_path: Path) -> tuple[str, ...] | None:
    """Every path the snapshot tarball lists, or None when it cannot be listed at all."""
    try:
        with tarfile.open(snapshot_path, "r:gz") as archive:
            return tuple(archive.getnames())
    except (OSError, tarfile.TarError, EOFError) as exc:
        logger.warning("Could not list {}: {}", snapshot_path, exc)
        return None


@pure
def registry_rows(sources: StepFactSources) -> tuple[RegisteredApp, ...] | None:
    """The captured app registry's rows, stamped with what the manifest says was pre-existing and seeded.

    None wherever the delivered set cannot be resolved: the capture is unreadable, the registry was
    not there to read, or the manifest could not say which rows the workspace already served -- in
    which case every template app would otherwise read as the agent's.
    """
    if not sources.apps_registry.is_readable or sources.manifest is None:
        return None
    preexisting = sources.manifest.preexisting_registrations
    if preexisting is None or not sources.manifest.is_registry_present:
        return None
    return evidence_collection.parse_apps_registry(
        sources.apps_registry.text, frozenset(preexisting), frozenset(sources.manifest.seeded_registrations)
    )


@pure
def driver_log_timestamps(log_text: str) -> tuple[str, str]:
    """The first and last timestamps a driver log carries, or two empty strings when it carries none.

    loguru writes `<time> | <level> | <name>:<line> - <message>`, and a multi-line message continues
    on lines carrying no such prefix, so only the lines with one are read.
    """
    stamps = [
        line.split(_DRIVER_LOG_FIELD_SEPARATOR, 1)[0].strip()
        for line in log_text.splitlines()
        if _DRIVER_LOG_FIELD_SEPARATOR in line and line[:1].isdigit()
    ]
    return (stamps[0], stamps[-1]) if stamps else ("", "")


@pure
def _step_metadata(result: TrialResult | None, step_index: int, step_name: str) -> dict[str, Any] | None:
    """What harbor recorded as one step's agent context metadata.

    A flat trial's one step is the trial's own agent result; a stepped trial's is the step result
    harbor appended under that name.
    """
    if result is None:
        return None
    agent_result = result.agent_result
    if not step_name:
        return dict(agent_result.metadata) if agent_result is not None and agent_result.metadata else None
    step_results = result.step_results or ()
    if step_index >= len(step_results):
        return None
    step_agent_result = step_results[step_index].agent_result
    return dict(step_agent_result.metadata) if step_agent_result is not None and step_agent_result.metadata else None


@pure
def _step_spend_input_tokens(result: TrialResult | None, step_index: int, step_name: str) -> tuple[int | None, ...]:
    """What harbor recorded as each step's published input-token delta, up to and including this step."""
    if result is None:
        return ()
    if not step_name:
        return (result.agent_result.n_input_tokens if result.agent_result is not None else None,)
    return tuple(
        step_result.agent_result.n_input_tokens if step_result.agent_result is not None else None
        for step_result in (result.step_results or ())[: step_index + 1]
    )


def load_step_fact_sources(
    step_paths: StepArtifactPaths,
    step_index: int,
    step_names_so_far: Sequence[str],
    result: TrialResult | None,
    previous_driver_log_last_timestamp: str,
) -> StepFactSources:
    """Everything one step recorded, read off its step directory."""
    agent_dir = step_paths.agent_dir
    verification_dir = agent_dir / evidence_collection.VERIFICATION_DIRNAME
    workers_dir = verification_dir / evidence_collection.WORKERS_DIRNAME
    state = _read_json_object(step_paths.state_path)
    manifest, manifest_block = _read_manifest(step_paths.evidence_manifest_path)
    tickets, tickets_failure_reason, is_tickets_present = _read_tickets(
        verification_dir / evidence_collection.TICKETS_FILENAME
    )
    case = _read_case_config(agent_dir / INSTRUCTION_FILENAME)
    listing_read = _read_file(workers_dir / evidence_collection.WORKER_LISTING_FILENAME)
    raw_captures, is_worker_captures_present = _read_json_array(
        workers_dir / evidence_collection.WORKER_CAPTURES_FILENAME
    )
    bundle_path = verification_dir / evidence_collection.DELIVERABLE_BUNDLE_FILENAME
    driver_events_read = _read_file(agent_dir / DRIVER_EVENTS_FILENAME)
    return StepFactSources(
        step_name=step_paths.step_name,
        step_index=step_index,
        step_names_so_far=tuple(step_names_so_far),
        incompletion_reason=describe_incompletion(result, state),
        spend_input_token_deltas=_step_spend_input_tokens(result, step_index, step_paths.step_name),
        previous_driver_log_last_timestamp=previous_driver_log_last_timestamp,
        state=state,
        case=case,
        trajectory=_read_json_object(agent_dir / TRAJECTORY_FILENAME),
        is_driver_events_present=driver_events_read.is_present,
        driver_events=parse_jsonl_objects(driver_events_read.text),
        driver_log=_read_file(agent_dir / DRIVER_LOG_FILENAME),
        step_metadata=_step_metadata(result, step_index, step_paths.step_name),
        snapshot_path=_newest_snapshot_path(agent_dir / SNAPSHOTS_DIRNAME),
        manifest=manifest,
        manifest_block=manifest_block,
        manifest_path=step_paths.evidence_manifest_path,
        apps_registry=_read_file(verification_dir / evidence_collection.APPS_REGISTRY_FILENAME),
        services=_read_file(verification_dir / evidence_collection.SERVICES_FILENAME),
        supervisord_conf=_read_file(verification_dir / evidence_collection.SUPERVISORD_CONF_FILENAME),
        isolated_instance_services=evidence_collection.parse_isolated_instance_services(
            _read_file(verification_dir / evidence_collection.ISOLATED_INSTANCE_SERVICES_FILENAME).text
        ),
        repo_state=_read_json_object(verification_dir / evidence_collection.REPO_STATE_FILENAME),
        bundle_path=bundle_path if bundle_path.is_file() else None,
        tickets=tickets,
        tickets_failure_reason=tickets_failure_reason,
        is_tickets_present=is_tickets_present,
        agent_inventory=evidence_collection.parse_worker_listing(listing_read.text)
        if listing_read.is_readable
        else (),
        is_agent_listing_present=listing_read.is_present,
        worker_listing_status=_parse_worker_listing_status(
            _read_json_object(workers_dir / evidence_collection.WORKER_LISTING_OUTCOME_FILENAME)
        ),
        worker_captures=parse_worker_captures(raw_captures) if raw_captures is not None else (),
        is_worker_captures_present=is_worker_captures_present,
        flows_by_slug=_read_flows(verification_dir / evidence_collection.FLOWS_DIRNAME),
        reward_details=_read_json_object(step_paths.reward_details_path),
        judge_flows_digest=_read_file(step_paths.derived_dir / JUDGE_FLOWS_DIGEST_FILENAME),
        judge_screenshots=_read_file(step_paths.derived_dir / JUDGE_SCREENSHOT_NAMES_FILENAME),
        judge_transcript=_read_file(step_paths.derived_dir / JUDGE_TRANSCRIPT_FILENAME),
        progress_summary=_read_file(step_paths.derived_dir / PROGRESS_SUMMARY_FILENAME),
        harness_failures=_read_file(step_paths.derived_dir / HARNESS_FAILURES_FILENAME),
        diagnostic_probe=_read_file(verification_dir / evidence_collection.DIAGNOSTIC_PROBE_FILENAME),
    )


def read_check_time_readings(sources: StepFactSources, work_dir: Path) -> CheckTimeReadings:
    """Open the step's archives. `work_dir` is a scratch directory the bundle is verified in, and is
    written to rather than read, so it must not be part of the job directory."""
    return CheckTimeReadings(
        snapshot_member_names=snapshot_member_names(sources.snapshot_path)
        if sources.snapshot_path is not None
        else None,
        bundle_byte_count=sources.bundle_path.stat().st_size if sources.bundle_path is not None else None,
        is_bundle_verified=sources.bundle_path is not None and is_bundle_verified(sources.bundle_path, work_dir),
    )


@pure
def _arm_harness(state: Mapping[str, Any] | None) -> str:
    arm = (state or {}).get("arm")
    harness_config = arm.get("harness_config") if isinstance(arm, Mapping) else None
    return str(harness_config.get("harness") or "") if isinstance(harness_config, Mapping) else ""


def load_trial_fact_sources(trial_dir: Path) -> TrialFactSources:
    """Every step of one trial, read off the job directory.

    Raises JobReadError for a result.json that is there but is not a harbor trial result, as
    `check-run` does: that is a job that cannot be read rather than a trial that failed.
    """
    result_path = TrialPaths(trial_dir=trial_dir).result_path
    result = load_trial_result(result_path)
    all_step_paths = resolve_step_artifact_paths(trial_dir, result)
    steps: list[StepFactSources] = []
    previous_log_last_timestamp = ""
    for step_index, step_paths in enumerate(all_step_paths):
        step = load_step_fact_sources(
            step_paths,
            step_index,
            [paths.step_name for paths in all_step_paths[: step_index + 1]],
            result,
            previous_log_last_timestamp,
        )
        steps.append(step)
        previous_log_last_timestamp = driver_log_timestamps(step.driver_log.text)[1]
    last_state = next((step.state for step in reversed(steps) if step.state is not None), None)
    return TrialFactSources(
        trial_name=trial_dir.name,
        case_id=str((last_state or {}).get("case_name") or ""),
        harness=_arm_harness(last_state),
        incompletion_reason=describe_incompletion(result, last_state),
        steps=tuple(steps),
    )

"""The trial-time evidence collection phase: everything that needs the workspace alive.

The verifier is a separate container that runs after the trial, by which time the nested workspace
sandbox has been destroyed. So anything that needs the live app -- the app registry, supervisord, an
HTTP probe, the agent's own tests -- is captured here, into ``/logs/agent/verification/``, and the
grade-time criteria score the *recorded* results. That split is what keeps ``harbor trial regrade``
cheap and pure: the unrepeatable half is captured once, the scoring policy can evolve.

Everything is written incrementally, so a phase that crashes or runs out of budget still leaves the
evidence collected up to that point. Every manifest entry carries a status where ``failed`` means the
workspace fell short and ``error`` means the harness could not find out -- an agent must never score
zero because the measuring instrument broke.
"""

import asyncio
import base64
import json
import posixpath
import re
import shlex
import time
import tomllib
from collections.abc import Mapping
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from datetime import datetime
from pathlib import Path
from typing import Final
from typing import assert_never

from harbor.environments.base import BaseEnvironment
from loguru import logger
from modal.exception import Error as ModalError
from pydantic import ConfigDict
from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.minds_evals import flow_runner
from imbue.minds_evals import forward_instance
from imbue.minds_evals import minds_bridge
from imbue.minds_evals import ui_flows
from imbue.minds_evals.data_types import CapturedFile
from imbue.minds_evals.data_types import CaseConfig
from imbue.minds_evals.data_types import CheckClass
from imbue.minds_evals.data_types import CheckStatus
from imbue.minds_evals.data_types import EntryRecord
from imbue.minds_evals.data_types import EvidenceEnv
from imbue.minds_evals.data_types import EvidenceManifest
from imbue.minds_evals.data_types import ExpandedExpectations
from imbue.minds_evals.data_types import GoalEntry
from imbue.minds_evals.data_types import GoalSatisfactionTiming
from imbue.minds_evals.data_types import HttpCheck
from imbue.minds_evals.data_types import ManifestEntry
from imbue.minds_evals.data_types import PhaseTiming
from imbue.minds_evals.data_types import ProcessCheck
from imbue.minds_evals.data_types import ProcessCheckKind
from imbue.minds_evals.data_types import REGISTERED_APPS_HTTP_TARGET
from imbue.minds_evals.data_types import RegisteredApp
from imbue.minds_evals.data_types import SkillInvocation
from imbue.minds_evals.data_types import TimingCheck
from imbue.minds_evals.data_types import TraceRecord
from imbue.minds_evals.data_types import TranscriptCapture
from imbue.minds_evals.data_types import TurnRecord
from imbue.minds_evals.data_types import UiFlowCheck
from imbue.minds_evals.data_types import WorkerCapture
from imbue.minds_evals.data_types import WorkerLaunch
from imbue.minds_evals.data_types import WorkerListing
from imbue.minds_evals.data_types import WorkerListingEntry
from imbue.minds_evals.data_types import WorkerState
from imbue.minds_evals.data_types import is_same_skill
from imbue.minds_evals.errors import TrajectoryDocumentError
from imbue.minds_evals.expectations import slugify
from imbue.minds_evals.resources.flow_step_protocol import StepReaction
from imbue.minds_evals.trajectory import parse_transcript_jsonl
from imbue.minds_evals.trajectory import parse_worker_document
from imbue.minds_evals.trajectory import scan_skill_invocations
from imbue.minds_evals.trajectory import scan_worker_launches
from imbue.mngr.primitives import AgentLifecycleState

# The bundle layout, relative to /logs/agent/. The directory is declared as an artifact in task.toml,
# and harbor re-materializes artifacts at their original absolute paths, so the verifier reads these
# at exactly the paths written here.
VERIFICATION_DIRNAME: Final[str] = "verification"
MANIFEST_FILENAME: Final[str] = "manifest.json"
TRACE_FILENAME: Final[str] = "trace.jsonl"
FILE_INVENTORY_FILENAME: Final[str] = "file_inventory.jsonl"
APPS_REGISTRY_FILENAME: Final[str] = "apps.toml"
SERVICES_FILENAME: Final[str] = "services.txt"
REPO_STATE_FILENAME: Final[str] = "repo_state.json"
DELIVERABLE_BUNDLE_FILENAME: Final[str] = "deliverable.bundle"
COMMON_TRANSCRIPT_FILENAME: Final[str] = "common_transcript.jsonl"
WORKSPACE_TRAJECTORY_FILENAME: Final[str] = "workspace_trajectory.json"
_TRANSCRIPT_STDERR_FILENAME: Final[str] = "transcript.err"
# The background workers launched during the trial (the chat agent's and, in turn, theirs), one
# directory each under the bundle.
WORKERS_DIRNAME: Final[str] = "workers"
WORKER_LISTING_FILENAME: Final[str] = "agents.json"
WORKER_TRAJECTORY_FILENAME: Final[str] = "trajectory.json"
WORKER_STREAM_FILENAME: Final[str] = "common_transcript.jsonl"
WORKER_REPORTS_DIRNAME: Final[str] = "reports"
_WORKER_STDERR_FILENAME: Final[str] = "transcript.err"
_WORKER_LISTING_STDERR_FILENAME: Final[str] = "list.err"
# A ceiling no real trial should reach, there to keep a runaway launch loop from consuming the phase
# budget; and how deep a worker's own workers are followed.
MAX_WORKER_COUNT: Final[int] = 100
MAX_WORKER_ROUNDS: Final[int] = 3
HTTP_DIRNAME: Final[str] = "http"
FLOWS_DIRNAME: Final[str] = "flows"
FLOW_LOG_FILENAME: Final[str] = "log.jsonl"

MANIFEST_SCHEMA_VERSION: Final[int] = 1

# The staging directory inside the workspace for evidence too large to ride the exec bridge (the file
# inventory, the git bundle); those files are rsynced into the box the way snapshots are.
WORKSPACE_STAGING_DIR: Final[str] = "/tmp/minds-evals-verification"

# Where the workspace repo lives in a stock workspace (supervisord's `directory=` and the app
# scaffold both hard-code it). Probed rather than assumed, but tried first so the common case is free.
DEFAULT_WORKSPACE_REPO_ROOT: Final[str] = "/home/user/workspace"
# The main supervisord config. Programs may be declared here or one per file under `<this>.d/`,
# the drop-in directory the template's own layout test pins its `[include]` glob to.
SUPERVISORD_CONF_RELATIVE_PATH: Final[str] = "system/supervisord.conf"
# Throwaway "isolated instance" servers record the registry rows they registered here, one state
# file per instance. Reading that record is how a delivered app is told from a preview.
ISOLATED_INSTANCES_RELATIVE_PATH: Final[str] = "data/.state/isolated-instances"
ISOLATED_INSTANCE_FILENAME: Final[str] = "instance.json"

# Bounds. rewardkit's judge silently drops any file over 1 MB, so every captured body, tail, and log
# here is capped by design rather than by luck.
MAX_INVENTORY_ENTRY_COUNT: Final[int] = 20_000
# The snapshot excludes plus .git: the inventory answers "what did the agent ship", and a repo's
# loose objects would crowd real deliverable files out of the entry cap while adding nothing (the
# committed history travels as the git bundle instead).
INVENTORY_EXCLUDES: Final[tuple[str, ...]] = (*minds_bridge.SNAPSHOT_EXCLUDES, ".git")
MAX_HTTP_BODY_BYTES: Final[int] = 256 * 1024
MAX_COMMAND_OUTPUT_CHARS: Final[int] = 4_000
# What `tail -c` is given: the same budget, but spent in bytes, because that is the only unit the
# shell can bound an arbitrary command's output in.
MAX_COMMAND_OUTPUT_BYTES: Final[int] = MAX_COMMAND_OUTPUT_CHARS
MAX_TRACE_OUTPUT_CHARS: Final[int] = 2_000

# Per-step bridge budgets, each additionally clamped to what is left of the phase deadline. The
# probe budget is public because the driver's pre-turn-1 registry snapshot is the same bridged
# exec the collector runs, against a workspace that has already booted.
PROBE_TIMEOUT_SECONDS: Final[int] = 120
_INVENTORY_TIMEOUT_SECONDS: Final[int] = 300
_TRANSCRIPT_TIMEOUT_SECONDS: Final[int] = 300
_WORKER_TIMEOUT_SECONDS: Final[int] = 120
_BUNDLE_TIMEOUT_SECONDS: Final[int] = 300
_TEST_COMMAND_TIMEOUT_SECONDS: Final[int] = 300
_HTTP_TIMEOUT_SECONDS: Final[int] = 60
_RSYNC_TIMEOUT_SECONDS: Final[int] = 600
# How long the forward proxy gets to start serving. It has to bind, then discover the workspace,
# then bring up its SSH tunnel, and it answers 503 throughout.
_FORWARD_READY_ATTEMPT_COUNT: Final[int] = 40
_FORWARD_READY_POLL_SECONDS: Final[float] = 3.0

# What a probe prints in a `*_status` section when the file that section reports on was there to
# read. Anything else -- including the empty section a probe that died mid-command leaves behind --
# means the file could not be read, which is a different claim from a file that was read and lists
# nothing.
STATUS_PRESENT: Final[str] = "present"

_SECTION_MARKER: Final[str] = "<<<MINDS_EVALS_SECTION:{}>>>"
_SECTION_PATTERN: Final[re.Pattern[str]] = re.compile(r"<<<MINDS_EVALS_SECTION:([a-z_]+)>>>\n?")

# Reasons recorded on non-passing entries, so a manifest reader never has to parse prose.
REASON_TIMEOUT: Final[str] = ui_flows.REASON_TIMEOUT
REASON_BRIDGE_FAILED: Final[str] = "bridge_failed"
REASON_REPO_NOT_FOUND: Final[str] = "repo_not_found"
REASON_REGISTRY_ABSENT: Final[str] = "registry_absent"
REASON_REGISTRY_UNREADABLE: Final[str] = "registry_unreadable"
# The pre-turn-1 registry snapshot could not be taken, so nothing in the registry can be told apart
# from what the workspace was already serving before the agent ran.
REASON_PREEXISTING_UNKNOWN: Final[str] = "preexisting_unknown"
REASON_SERVICES_UNREADABLE: Final[str] = "services_unreadable"
# supervisord is running programs that the config we captured does not declare, so the capture
# missed part of the config and cannot say which program owns a row.
REASON_SUPERVISORD_CONF_UNREADABLE: Final[str] = "supervisord_conf_unreadable"
REASON_PROBE_UNAVAILABLE: Final[str] = "probe_unavailable"
REASON_NO_REGISTERED_APPS: Final[str] = "no_registered_apps"
REASON_TARGET_NOT_REGISTERED: Final[str] = "target_not_registered"
REASON_WRONG_STATUS: Final[str] = "wrong_status"
REASON_BODY_MISMATCH: Final[str] = "body_mismatch"
REASON_SERVICE_NOT_RUNNING: Final[str] = "service_not_running"
REASON_NO_SUPERVISED_PROGRAM: Final[str] = "no_supervised_program"
REASON_TOO_FEW_APPS: Final[str] = "too_few_apps"
REASON_NONZERO_EXIT: Final[str] = "nonzero_exit"
# Why a process check did not pass. The last two are the instrument failing rather than the agent:
# with no transcript, or one holding no record of the agent, there is nothing to read its own work
# off.
REASON_SKILL_NOT_INVOKED: Final[str] = "skill_not_invoked"
REASON_FORBIDDEN_SKILL_INVOKED: Final[str] = "forbidden_skill_invoked"
REASON_WORKER_LAUNCHES_EXCEEDED: Final[str] = "worker_launches_exceeded"
REASON_TRANSCRIPT_UNCAPTURED: Final[str] = "transcript_uncaptured"
REASON_TRANSCRIPT_EMPTY: Final[str] = "transcript_empty"
# Why a measured time does not stand. Neither is the instrument failing: a client who was never
# satisfied took infinitely long, and a prerequisite class that fell short means the trial was fast
# at something other than what the case commissioned. Both are the agent's, and score zero.
REASON_GOAL_NEVER_SATISFIED: Final[str] = "goal_never_satisfied"
REASON_TIMING_PREREQUISITE_FAILED: Final[str] = "timing_prerequisite_failed"
# Why a transcript file did not make it out of the workspace. These are recorded on the driver's
# trial metadata, not in the manifest: the transcript is the trial's record, not outcome evidence.
REASON_NOT_ATTEMPTED: Final[str] = "not_attempted"
REASON_TRANSCRIPT_COMMAND_FAILED: Final[str] = "transcript_command_failed"
REASON_PULL_FAILED: Final[str] = "pull_failed"
REASON_DOWNLOAD_FAILED: Final[str] = "download_failed"
REASON_NO_REPORT_PATH: Final[str] = "no_report_path"

# The name the oracle's fabricated evidence gives the app it pretends was delivered, and the
# template rows it pretends were already there, so the fabricated bundle exercises the same
# delivered-versus-pre-existing resolution a live trial does.
_ORACLE_PREEXISTING_APPS: Final[tuple[tuple[str, str], ...]] = (
    ("system_interface", "http://localhost:8000"),
    ("terminal", "http://localhost:7681"),
)
# The call the oracle's fabricated evidence says each required skill was invoked from.
_ORACLE_SKILL_CALL_ID: Final[str] = "call-oracle-skill"
_ORACLE_APP_NAME: Final[str] = "delivered-app"
_ORACLE_APP_URL: Final[str] = "http://localhost:8080"
_ORACLE_APP_LABEL: Final[str] = "delivered-app-o1r2a3c4"

# Walks the workspace home tree once and writes the inventory as JSONL. Run as an in-workspace python
# program rather than a `find` pipeline so that paths containing quotes or newlines are escaped
# correctly, and so the entry cap is applied where the walk happens.
_INVENTORY_PROGRAM: Final[str] = """
import json, os
excludes = set({excludes!r})
root = os.path.expanduser("~")
limit = {limit}
count = 0
os.makedirs({staging!r}, exist_ok=True)
with open(os.path.join({staging!r}, {filename!r}), "w") as handle:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in excludes]
        for name in filenames:
            full = os.path.join(dirpath, name)
            try:
                stat_result = os.lstat(full)
            except OSError:
                continue
            handle.write(json.dumps({{
                "path": os.path.relpath(full, root),
                "size_bytes": stat_result.st_size,
                "mtime": stat_result.st_mtime,
            }}) + "\\n")
            count += 1
            if count >= limit:
                break
        if count >= limit:
            break
print(count)
"""


@pure
def file_inventory_command() -> str:
    """The inventory walk, shipped base64-encoded and decoded in the workspace.

    Every other probe here is a single line of shell, but this one is a multi-line python program;
    encoding it keeps the command a single line of plain characters so no layer of the bridge --
    which quotes the command twice on its way in -- can mangle it.
    """
    program = _INVENTORY_PROGRAM.format(
        excludes=list(INVENTORY_EXCLUDES),
        limit=MAX_INVENTORY_ENTRY_COUNT,
        staging=WORKSPACE_STAGING_DIR,
        filename=FILE_INVENTORY_FILENAME,
    )
    encoded = base64.b64encode(program.encode()).decode("ascii")
    return "printf '%s' {} | base64 -d | python3 -".format(shlex.quote(encoded))


@pure
def section_marker(name: str) -> str:
    """The delimiter a multi-section probe prints before each answer, so one bridged exec can carry
    several. Exposed because the driver builds a sectioned probe of its own during clone prep."""
    return _SECTION_MARKER.format(name)


def box_verification_dir() -> str:
    return "{}/{}".format(minds_bridge.BOX_LOGS_DIR, VERIFICATION_DIRNAME)


async def ensure_evidence_dir(environment: BaseEnvironment) -> None:
    """Create the declared evidence artifact directory in the box, empty if need be.

    Called at setup, before anything can fail. harbor records a missing declared artifact path as a
    FAILED entry and `harbor trial regrade` refuses any trial carrying one, while an empty directory
    is tolerated -- so a directory that only appears when collection runs would make every trial
    that died earlier permanently non-regradable. Never declare an artifact path the driver might
    not produce.
    """
    await environment.exec(
        "mkdir -p {}/{}".format(box_verification_dir(), HTTP_DIRNAME), timeout_sec=PROBE_TIMEOUT_SECONDS
    )


@pure
def split_sections(output: str) -> dict[str, str]:
    """Split a multi-section probe's stdout on its section markers.

    One bridged exec costs a Modal round trip, so the collector asks several questions per command
    and separates the answers here rather than paying for a call each.

    The FIRST occurrence of a marker wins. Some sections carry content the agent under test controls
    (an HTTP response body, a test command's output), so a later duplicate marker must never be able
    to overwrite an earlier, harness-emitted section and forge a passing probe.
    """
    parts = _SECTION_PATTERN.split(output)
    sections: dict[str, str] = {}
    # split() yields [preamble, name, body, name, body, ...]; the preamble is shell noise.
    for index in range(1, len(parts) - 1, 2):
        sections.setdefault(parts[index], parts[index + 1])
    return sections


@pure
def parse_apps_registry(
    registry_text: str, preexisting_registrations: frozenset[str]
) -> tuple[RegisteredApp, ...] | None:
    """The registered apps out of data/.state/apps.toml (an array of {name, url, label} tables).

    None means the registry could not be read at all (unparseable, or not the shape it should be),
    which the caller records as ERROR. An empty tuple is a different claim entirely: the registry was
    read and holds nothing, which counts against the agent.

    ``preexisting_registrations`` is what the workspace already served before the agent ran (see
    ``resolve_preexisting_registrations``); the rows it names are stamped as such. A caller that
    could not determine that set must not pass an empty one and read every row as delivered;
    ``EvidenceCollector`` leaves the registry unresolved instead.
    """
    try:
        parsed = tomllib.loads(registry_text)
    except tomllib.TOMLDecodeError as exc:
        logger.warning("Could not parse the workspace app registry: {}", exc)
        return None
    raw_apps = parsed.get("apps")
    if raw_apps is None:
        return ()
    if not isinstance(raw_apps, list):
        logger.warning("The workspace app registry's 'apps' key is not an array of tables")
        return None
    apps: list[RegisteredApp] = []
    for raw_app in raw_apps:
        if not isinstance(raw_app, dict):
            continue
        name = str(raw_app.get("name") or "").strip()
        if not name:
            continue
        apps.append(
            RegisteredApp(
                name=name,
                url=str(raw_app.get("url") or ""),
                label=str(raw_app.get("label") or ""),
                is_preexisting=name in preexisting_registrations,
                is_internal=bool(raw_app.get("internal")),
            )
        )
    return tuple(apps)


@pure
def parse_registry_names(registry_text: str) -> frozenset[str] | None:
    """Just the names in an app registry, without resolving which of them were delivered.

    What the driver's boot-time snapshot needs: at that point the question is only which rows exist
    yet, and no pre-existing set is available to classify them against. None means the registry
    could not be read, which is not the same claim as a registry that lists nothing.
    """
    apps = parse_apps_registry(registry_text, frozenset())
    if apps is None:
        return None
    return frozenset(app.name for app in apps)


@pure
def is_registry_status_present(sections: Mapping[str, str]) -> bool:
    """Whether the workspace-state probe found the app registry file at all."""
    return sections.get("registry_status", "").strip() == STATUS_PRESENT


@pure
def parse_registry_snapshot(output: str) -> frozenset[str] | None:
    """The apps a workspace already serves, out of one `workspace_state_command` run.

    What the driver's pre-turn-1 snapshot reads. Both halves of the pre-existing set come out of
    this single probe -- the registry it captured and the supervisord config it catted, which
    before the first turn is still the pinned template's file verbatim -- so they are decoded here,
    in the module that prints the probe's sections. See `resolve_preexisting_registrations` for why
    one source is not enough.

    None means unknown, never empty: a registry that is not there yet, one that could not be
    parsed, or a supervisord config this probe could not read whole. Callers turn it into
    ``preexisting_unknown`` and leave the trial unmeasured rather than scoring it, which is the
    whole point -- an empty set would say the workspace served nothing before the agent ran, and
    every template app would become the agent's deliverable. The config half is included because
    it has no fallback of its own: a template app that had not registered its port yet is missing
    from both halves at once.
    """
    sections = split_sections(output)
    if not is_registry_status_present(sections):
        return None
    supervisord_conf = sections.get("supervisord", "")
    if is_supervisord_capture_broken(supervisord_conf, parse_service_states(sections.get("services", ""))):
        return None
    return resolve_preexisting_registrations(
        parse_registry_names(sections.get("registry", "")),
        frozenset(parse_supervised_registrations(supervisord_conf)),
    )


# The states supervisord reports for a program. Used to tell its status listing apart from an error
# message: `supervisorctl status` exits nonzero merely because a program is down, so the exit code
# says nothing about whether we managed to ask -- but a line naming a real state does.
_SUPERVISOR_STATES: Final[frozenset[str]] = frozenset(
    {"RUNNING", "STARTING", "STOPPED", "STOPPING", "BACKOFF", "EXITED", "FATAL", "UNKNOWN"}
)


@pure
def parse_service_states(services_text: str) -> dict[str, str]:
    """Program name -> state out of `supervisorctl status` output ("name  RUNNING  pid ...").

    Lines that do not name a supervisord state are dropped, so an error message ("connection
    refused", "command not found") yields nothing at all rather than junk entries -- which is what
    lets the caller tell a broken instrument from a stopped service.
    """
    states: dict[str, str] = {}
    for line in services_text.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[1].upper() in _SUPERVISOR_STATES:
            states[fields[0]] = fields[1].upper()
    return states


# The forward_port.py call an app's supervisord program block chains before its own start command.
# Either flag order is accepted: the app scaffold writes --url first, the isolated-instance runner
# writes --name first, and a hand-written block may do either. An app with a manifest registers
# through ``--manifest <app.toml>`` with no ``--name`` (the name lives in the manifest); its program
# is named after the app, so the block's own program name is the registration.
# A call's flags run to the next shell chain operator (``&&``, ``;``, ``||``: the chain to the
# following call or to the app's own start command, whose flags must not be read as the
# registration's) or to the end of the line.
_FORWARD_PORT_CALL_PATTERN: Final[re.Pattern[str]] = re.compile(r"forward_port\.py(?P<flags>[^\n&;|]*)")
_FORWARD_PORT_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"--name\s+([\w-]+)")
_FORWARD_PORT_MANIFEST_PATTERN: Final[re.Pattern[str]] = re.compile(r"--manifest\s+\S+")
_PROGRAM_SECTION_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\[program:([^\]]+)\]", re.MULTILINE)
# A program's block ends at the next section of any kind, not the next program: an
# ``[eventlistener:*]`` or a second ``[include]`` between two programs belongs to neither.
_SECTION_HEADER_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\[[^\]]+\]", re.MULTILINE)
# Everything supervisord supervises and lists in `supervisorctl status`, whatever file declares it.
_SUPERVISED_SECTION_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\[(?:program|eventlistener):", re.MULTILINE)


@pure
def _without_comment_lines(supervisord_conf: str) -> str:
    """The config with its full-line comments removed, the two markers supervisord's parser takes."""
    return "\n".join(line for line in supervisord_conf.splitlines() if not line.lstrip().startswith(("#", ";")))


@pure
def _registrations_in_block(block: str, program_name: str) -> list[str]:
    """The registry names the forward_port.py calls in one program block register, in call order."""
    registrations: list[str] = []
    for call in _FORWARD_PORT_CALL_PATTERN.finditer(block):
        flags = call.group("flags")
        name_match = _FORWARD_PORT_NAME_PATTERN.search(flags)
        if name_match is not None:
            registrations.append(name_match.group(1))
        elif _FORWARD_PORT_MANIFEST_PATTERN.search(flags) is not None:
            registrations.append(program_name)
        else:
            pass
    return registrations


@pure
def parse_supervised_registrations(supervisord_conf: str) -> dict[str, str]:
    """Registry name -> the supervisord program whose block registers it.

    The join goes through the `forward_port.py` invocations inside each `[program:*]` block rather
    than through name equality, because the two are not the same thing: a multi-port app registers
    extra origin-label rows (`<name>-admin`) that have no program of their own, and a program is
    free to register a row under any name. The workspace template joins the two the same way, in
    `.agents/skills/migrate-workspace/scripts/migrate_workspace.py`.

    Takes the whole capture, which is the main config followed by its drop-ins. Comment lines are
    dropped first: a block runs to the next section header, so a drop-in's leading prose -- which
    routinely names `forward_port.py` -- would otherwise be read as part of the last program of the
    file before it.
    """
    supervisord_conf = _without_comment_lines(supervisord_conf)
    program_by_registration: dict[str, str] = {}
    section_starts = [match.start() for match in _SECTION_HEADER_PATTERN.finditer(supervisord_conf)]
    for match in _PROGRAM_SECTION_PATTERN.finditer(supervisord_conf):
        block_end = next((start for start in section_starts if start > match.start()), len(supervisord_conf))
        block = supervisord_conf[match.end() : block_end]
        program_name = match.group(1).strip()
        for registration in _registrations_in_block(block, program_name):
            program_by_registration.setdefault(registration, program_name)
    return program_by_registration


@pure
def is_supervisord_capture_broken(supervisord_conf: str, service_state_by_name: Mapping[str, str]) -> bool:
    """Whether the captured supervisord config cannot be the one supervisord is actually running.

    supervisord takes its programs from the config this capture claims to have read, so a status
    listing that names programs while the capture declares none is not an app-free workspace: it is
    a read that missed part of the config. A template layout the capture does not follow looks
    exactly like this from here, which is the failure that otherwise passes for a true negative --
    a workspace serving nothing answers with an empty join too.

    Keyed on declared sections rather than on ``forward_port.py`` calls, so it holds for a template
    whose apps all register their ports at runtime and whose config therefore registers nothing.
    An unreadable ``supervisorctl status`` (empty mapping) proves nothing either way and is already
    reported as its own broken instrument.
    """
    if not service_state_by_name:
        return False
    return not _SUPERVISED_SECTION_PATTERN.search(_without_comment_lines(supervisord_conf))


@pure
def resolve_preexisting_registrations(
    registry_names: frozenset[str] | None, config_registrations: frozenset[str]
) -> frozenset[str] | None:
    """What the workspace already served before the agent ran, or None if that cannot be told.

    Both arguments are read from the same pre-turn-1 snapshot, because neither alone is complete:

    - ``registry_names`` is the app registry as it actually stood. A measurement rather than an
      inference, and the only source that sees a template app which registers from inside the
      program it runs (its own entry point, or a launcher script) rather than from a
      ``forward_port.py`` call in the config itself. The terminal does exactly that, as do the
      owner-exec and vm-exec daemons, and counting one as a deliverable is the failure this
      resolution exists to prevent.
    - ``config_registrations`` is what the workspace's own supervisord config registers, drop-ins
      included, joined through its ``forward_port.py`` invocations (``--name``, or the block's own
      program name for a ``--manifest`` registration). It covers a template app whose service is
      slow enough that it had not registered its port yet when the snapshot was taken: the files
      are on disk from the moment the workspace is cloned, whatever its services are doing. Directory
      names under ``system/apps/`` would not do -- a registry name is what the app hands
      ``forward_port.py`` (a ``--name`` flag, or the name in its ``--manifest``), and a multi-port
      app registers extra origin-label rows that correspond to no directory at all.

    The registry is therefore the half that must be readable; the config half only ever adds names,
    and contributes nothing when the probe came back without it.
    """
    if registry_names is None:
        return None
    return registry_names | config_registrations


@pure
def parse_isolated_instance_services(instances_text: str) -> frozenset[str]:
    """The registry rows owned by throwaway "isolated instance" servers, from their own state files.

    Read from that record rather than matched by name pattern: the service names are supplied by
    whoever started the instance, so a pattern would both miss arbitrary ones and wrongly exclude a
    real deliverable that happens to be called something like `recipes-test`. A cleanly torn-down
    instance has already deregistered its rows and removed its state, so what remains here is
    exactly the abandoned throwaways that would otherwise be mistaken for deliverables.
    """
    services: set[str] = set()
    decoder = json.JSONDecoder()
    position = 0
    text = instances_text.strip()
    # The probe concatenates every instance.json, so decode one object at a time.
    while position < len(text):
        try:
            state, offset = decoder.raw_decode(text, position)
        except ValueError:
            break
        if isinstance(state, dict):
            for service in state.get("services") or []:
                if isinstance(service, str) and service.strip():
                    services.add(service.strip())
        next_position = offset
        while next_position < len(text) and text[next_position].isspace():
            next_position += 1
        position = next_position
    return frozenset(services)


@pure
def resolve_delivered_apps(
    registered_apps: Sequence[RegisteredApp], isolated_instance_services: frozenset[str]
) -> tuple[RegisteredApp, ...]:
    """The registry rows that represent the case's deliverable.

    Narrower than "not pre-existing", in two ways a live trial proved matter:

    - Rows the registry marks ``internal`` are machinery that forwards a port but has no page of its
      own to show (the owner-exec daemon, for one). They answer 404 on ``/`` by design, so counting
      one both inflates the delivered count and fails the root-path probe on something nobody
      shipped.
    - A throwaway preview server registers through the same path and leaves its row behind when
      abandoned, so counting it charges the agent for a dead port that was never the deliverable.
    """
    return tuple(
        app
        for app in registered_apps
        if not app.is_preexisting and not app.is_internal and app.name not in isolated_instance_services
    )


@pure
def _entry(
    entry_id: str,
    check_class: CheckClass,
    status: CheckStatus,
    reason: str,
    detail: str,
    evidence_path: str,
) -> ManifestEntry:
    return ManifestEntry(
        entry_id=entry_id,
        check_class=check_class,
        status=status,
        env=EvidenceEnv.LIVE,
        reason=reason,
        detail=detail,
        evidence_path=evidence_path,
    )


@pure
def _flow_entry(check: UiFlowCheck, status: CheckStatus, reason: str, detail: str) -> ManifestEntry:
    """One flow's manifest entry. Its evidence path is the flow's DIRECTORY: the grade-time
    pre-step joins entries back to their captured steps and screenshots through that basename."""
    return ManifestEntry(
        entry_id=check.check_id,
        check_class=CheckClass.UI_FLOWS,
        status=status,
        env=EvidenceEnv.LIVE,
        reason=reason,
        detail=detail,
        evidence_path="{}/{}/{}".format(VERIFICATION_DIRNAME, FLOWS_DIRNAME, slugify(check.name)),
    )


@pure
def _invocation_sites(invocations: Sequence[SkillInvocation]) -> str:
    """Where a skill was invoked, as the manifest names it: each call, with the step it sat in when
    the record carries one (a stream record names no step)."""
    return ", ".join(
        "{}{}".format(
            invocation.tool_call_id or "?", " (step {})".format(invocation.step_id) if invocation.step_id else ""
        )
        for invocation in invocations
    )


@pure
def _invoked_skill_names(invocations: Sequence[SkillInvocation]) -> str:
    """Every skill the transcript shows, each once, so a check that found nothing still says what the
    agent did reach for -- which is how a renamed skill is told from one the agent never used."""
    names = list(dict.fromkeys(invocation.name for invocation in invocations))
    return ", ".join(names) if names else "none"


@pure
def _process_entry(
    check: ProcessCheck, invocations: Sequence[SkillInvocation], launches: Sequence[WorkerLaunch]
) -> ManifestEntry:
    """One process check's verdict, read off the agent's own transcript.

    Every outcome here is the agent's: the transcript is the record of what it did, so a check that
    can be evaluated at all is FAILED rather than ERROR when it does not hold.
    """
    match check.kind:
        case ProcessCheckKind.REQUIRED_SKILL:
            matching = [invocation for invocation in invocations if is_same_skill(check.skill, invocation.name)]
            return _entry(
                check.check_id,
                CheckClass.PROCESS,
                CheckStatus.PASSED if matching else CheckStatus.FAILED,
                "" if matching else REASON_SKILL_NOT_INVOKED,
                "{} invoked at {}".format(check.skill, _invocation_sites(matching))
                if matching
                else "{} was never invoked; the transcript shows: {}".format(
                    check.skill, _invoked_skill_names(invocations)
                ),
                "",
            )
        case ProcessCheckKind.FORBIDDEN_SKILL:
            matching = [invocation for invocation in invocations if is_same_skill(check.skill, invocation.name)]
            return _entry(
                check.check_id,
                CheckClass.PROCESS,
                CheckStatus.FAILED if matching else CheckStatus.PASSED,
                REASON_FORBIDDEN_SKILL_INVOKED if matching else "",
                "{} invoked at {}".format(check.skill, _invocation_sites(matching))
                if matching
                else "{} was never invoked".format(check.skill),
                "",
            )
        case ProcessCheckKind.MAX_WORKER_LAUNCHES:
            is_within_cap = len(launches) <= check.max_worker_launches
            return _entry(
                check.check_id,
                CheckClass.PROCESS,
                CheckStatus.PASSED if is_within_cap else CheckStatus.FAILED,
                "" if is_within_cap else REASON_WORKER_LAUNCHES_EXCEEDED,
                "{} worker(s) launched against a cap of {}{}".format(
                    len(launches),
                    check.max_worker_launches,
                    ": {}".format(", ".join(launch.name for launch in launches)) if launches else "",
                ),
                "",
            )
        case _ as unreachable:
            assert_never(unreachable)


@pure
def measure_goal_satisfaction_timing(
    entry_records: Sequence[EntryRecord], turn_records: Sequence[TurnRecord]
) -> GoalSatisfactionTiming | None:
    """How long the trial took to get its goal-holding client to say the goal was met, or None when
    no entry ever did.

    Anchored on the case's FIRST client message, not on the satisfying entry's own: what the timing
    class measures is how long the client waited between asking and being shown, and the earlier
    entries are part of that wait. The records accumulate across a stepped case's steps, the way
    ``conversation_seconds`` does, so a later step's figure spans from the case's first message.

    Closed by the LAST satisfaction rather than the first, so a case that holds out for several
    goals in turn is measured to the one it finished on -- and, on a stepped case, to this step's.
    """
    if not turn_records:
        return None
    satisfaction = next(
        (
            (record.satisfied_at, record.satisfied_at_turn)
            for record in reversed(tuple(entry_records))
            if record.satisfied_at and record.satisfied_at_turn is not None
        ),
        None,
    )
    if satisfaction is None:
        return None
    satisfied_at, satisfied_at_turn = satisfaction
    started_at = datetime.fromisoformat(turn_records[0].sent_at)
    return GoalSatisfactionTiming(
        seconds=round((datetime.fromisoformat(satisfied_at) - started_at).total_seconds(), 1),
        turn_index=satisfied_at_turn,
    )


@pure
def _timing_entry(
    check: TimingCheck,
    timing: GoalSatisfactionTiming | None,
    failed_prerequisite_classes: Sequence[CheckClass],
) -> ManifestEntry:
    """The timing class's one entry: the measurement the grade-time criterion scores on its curve,
    plus the prose a reader needs to see why it scored what it did.

    PASSED means there is a time to score, not that the time was good -- the class is continuous and
    the curve is applied at grade time, so a trial slower than the slow anchor passes here and
    scores 0.0 there. FAILED means there is nothing to score on the curve: a client that never said
    the goal was met, or a prerequisite class that fell short. Neither is an ERROR: a client who was
    never satisfied took unboundedly long, which is a measurement of the agent.

    A time that a prerequisite zeroed is still recorded in ``value``, so the entry says both how
    fast the trial was and why being fast earned it nothing.
    """
    anchors = "fast {:.0f}s, slow {:.0f}s".format(check.fast_seconds, check.slow_seconds)
    if timing is None:
        return ManifestEntry(
            entry_id=check.check_id,
            check_class=CheckClass.TIMING,
            status=CheckStatus.FAILED,
            env=EvidenceEnv.LIVE,
            reason=REASON_GOAL_NEVER_SATISFIED,
            detail="no entry's client ever said its goal was met, so the time to it is unbounded ({})".format(anchors),
            evidence_path="",
            value=None,
        )
    measured = "{:.1f}s to the reply to client message {} ({})".format(timing.seconds, timing.turn_index, anchors)
    if failed_prerequisite_classes:
        return ManifestEntry(
            entry_id=check.check_id,
            check_class=CheckClass.TIMING,
            status=CheckStatus.FAILED,
            env=EvidenceEnv.LIVE,
            reason=REASON_TIMING_PREREQUISITE_FAILED,
            detail="{}, but the time does not count: a required check failed in the {} class(es)".format(
                measured, ", ".join(check_class.value for check_class in failed_prerequisite_classes)
            ),
            evidence_path="",
            value=timing.seconds,
        )
    return ManifestEntry(
        entry_id=check.check_id,
        check_class=CheckClass.TIMING,
        status=CheckStatus.PASSED,
        env=EvidenceEnv.LIVE,
        reason="",
        detail=measured,
        evidence_path="",
        value=timing.seconds,
    )


@pure
def supervisord_config_capture_command(repo_root: str) -> str:
    """Shell that prints a workspace's supervisord config: the main file, then its drop-ins.

    ``repo_root`` is a shell word naming the repo root -- a quoted literal, or a variable
    reference the surrounding script has already set.

    The drop-ins are ``<config>.d/*.conf``. That directory is the default template's convention,
    pinned by its own layout test to be what the config's ``[include]`` glob names, so the capture
    assumes it rather than parsing the glob out of the config.

    A template that declares every program in a drop-in leaves the main config with no
    ``[program:*]`` section at all, so the registration join comes back empty. The service-health
    check mostly rides that out on its same-name fallback; what does not is a multi-port app's
    extra origin rows, and `resolve_preexisting_registrations`, which has no fallback and so
    credits an unregistered template app to the agent. The failure is silent either way, because
    an empty result is also what an honestly app-free workspace gives.
    """
    shell = (
        "conf={root}/{conf}; "
        # Every file is followed by a newline, so a file boundary is always a line boundary: a
        # config whose last line is unterminated would otherwise run into the next file's first
        # line, and a `[program:*]` header that is no longer at the start of a line is not one
        # the block scan can see.
        "cat \"$conf\" 2>/dev/null; printf '\\n'; "
        # The glob is applied to the quoted path, so a repo root containing a space stays one
        # path; with no drop-in directory the pattern stays literal and the -f test skips it.
        'for path in "$conf".d/*.conf; do [ -f "$path" ] || continue; cat "$path"; printf \'\\n\'; done'
    )
    return shell.format(root=repo_root, conf=SUPERVISORD_CONF_RELATIVE_PATH)


@pure
def workspace_state_command() -> str:
    """One command answering everything the always-on capture needs: where the delivered repo is,
    what the app registry says, and what supervisord reports.

    Two readers decode its sections: the collector's always-on capture, and the driver's pre-turn-1
    snapshot of what the workspace already served (`parse_registry_snapshot`). A section added or
    renamed here has to keep both in step.
    """
    # Registry presence is reported separately from its contents: "the file is not there" is the
    # harness failing to measure, while "the file is there and lists no delivered app" is the agent
    # shipping nothing. Collapsing both into an empty capture would turn the very failure this eval
    # exists to catch into a harness error.
    return (
        'root=""; '
        "if [ -d {default}/.git ]; then root={default}; else "
        'root=$(find "$HOME" -maxdepth 4 -type d -path "*/system/vendor" 2>/dev/null | head -n 1); '
        "root=${{root%/system/vendor}}; fi; "
        'registry="$root/{registry_path}"; '
        "printf '{repo_marker}\\n'; printf '%s\\n' \"$root\"; "
        "printf '{registry_status_marker}\\n'; "
        "if [ -n \"$root\" ] && [ -f \"$registry\" ]; then printf '{present}\\n'; else printf 'absent\\n'; fi; "
        "printf '{registry_marker}\\n'; "
        'if [ -n "$root" ]; then cat "$registry" 2>/dev/null; fi; '
        "printf '{services_marker}\\n'; "
        "supervisorctl status 2>&1; "
        # The supervisord config and the isolated-instance state say which registry rows are
        # actually delivered apps; both ride this same exec rather than costing round trips of
        # their own.
        "printf '{supervisord_marker}\\n'; "
        'if [ -n "$root" ]; then {supervisord_capture}; fi; '
        "printf '{instances_marker}\\n'; "
        'if [ -n "$root" ]; then find "$root/{instances_path}" -name {instance_file} '
        "-exec cat {{}} + 2>/dev/null; fi; "
        "exit 0"
    ).format(
        default=DEFAULT_WORKSPACE_REPO_ROOT,
        present=STATUS_PRESENT,
        repo_marker=_SECTION_MARKER.format("repo_root"),
        registry_status_marker=_SECTION_MARKER.format("registry_status"),
        registry_marker=_SECTION_MARKER.format("registry"),
        services_marker=_SECTION_MARKER.format("services"),
        supervisord_marker=_SECTION_MARKER.format("supervisord"),
        instances_marker=_SECTION_MARKER.format("isolated_instances"),
        registry_path=minds_bridge.WORKSPACE_APPS_REGISTRY,
        supervisord_capture=supervisord_config_capture_command('"$root"'),
        instances_path=ISOLATED_INSTANCES_RELATIVE_PATH,
        instance_file=shlex.quote(ISOLATED_INSTANCE_FILENAME),
    )


@pure
def transcript_capture_command(chat_agent_id: str) -> str:
    """Write the chat agent's common transcript, as the raw stream and as the ATIF document mngr builds
    from it, into the workspace staging directory, reporting each command's exit code separately.

    Only the exit codes and a stderr tail ride the exec envelope; the files themselves are pulled
    out afterwards, so a long trial's transcript never has to fit through a command's stdout. On a
    workspace whose mngr predates ATIF the stream command still answers (with the legacy-shaped
    records its emitter wrote) while the document command fails, which is why each exit code is
    reported on its own.
    """
    return (
        "mkdir -p {staging}; "
        "mngr transcript {agent} --headless --format jsonl > {staging}/{stream} 2> {staging}/{stderr}; "
        "printf '{stream_marker}\\n%s\\n' \"$?\"; "
        "mngr transcript {agent} --headless --format atif --output {staging}/{document} 2>> {staging}/{stderr}; "
        "printf '{document_marker}\\n%s\\n' \"$?\"; "
        "printf '{stderr_marker}\\n'; tail -c {limit} {staging}/{stderr} 2>/dev/null; "
        "exit 0"
    ).format(
        staging=WORKSPACE_STAGING_DIR,
        agent=shlex.quote(chat_agent_id),
        stream=COMMON_TRANSCRIPT_FILENAME,
        document=WORKSPACE_TRAJECTORY_FILENAME,
        stderr=_TRANSCRIPT_STDERR_FILENAME,
        stream_marker=_SECTION_MARKER.format("stream_exit"),
        document_marker=_SECTION_MARKER.format("document_exit"),
        stderr_marker=_SECTION_MARKER.format("stderr"),
        limit=MAX_COMMAND_OUTPUT_BYTES,
    )


@pure
def worker_listing_command() -> str:
    """List the workspace's agents, printing the listing itself so the collector can resolve each
    worker's id, state, and work dir before it captures them."""
    return (
        "mkdir -p {workers}; "
        "mngr list --headless --format json > {workers}/{listing} 2> {workers}/{stderr}; "
        "printf '{exit_marker}\\n%s\\n' \"$?\"; "
        "printf '{listing_marker}\\n'; cat {workers}/{listing} 2>/dev/null; "
        "printf '{stderr_marker}\\n'; tail -c {limit} {workers}/{stderr} 2>/dev/null; "
        "exit 0"
    ).format(
        workers="{}/{}".format(WORKSPACE_STAGING_DIR, WORKERS_DIRNAME),
        listing=WORKER_LISTING_FILENAME,
        stderr=_WORKER_LISTING_STDERR_FILENAME,
        exit_marker=_SECTION_MARKER.format("list_exit"),
        listing_marker=_SECTION_MARKER.format("listing"),
        stderr_marker=_SECTION_MARKER.format("stderr"),
        limit=MAX_COMMAND_OUTPUT_BYTES,
    )


@pure
def worker_capture_command(name: str, agent_id: str, lead_work_dir: str, task_file: str) -> str:
    """Write one worker's ATIF document, its stream, and the report it pushed back into the staging
    directory, reporting each part on its own.

    `--preserved` keeps mngr's normal live lookup and adds its caller-local preservation archive,
    which is where a worker destroyed after finishing still has a stream. The target is the
    listing's exact agent id whenever the listing has one: a launch records only a name, and a name
    a destroyed worker released can be taken by another agent, so a name resolves to whoever holds
    it now. The identity recorded in evidence is the one in the captured document, not the name.

    Each part retries without `--preserved` when the flagged form fails, because the workspace runs
    the mngr its image was pinned to, which can be older than the flag: without the retry an mngr
    that rejects the option loses a live worker's evidence too. `||` makes the reported exit code
    the retry's when the first attempt failed, so a part that fails for a real reason still says so.

    The report is read from the lead's side: the launch-task contract has the worker push it to the
    path named in the task file's frontmatter, under the lead's own work dir.
    """
    worker_dir = "{}/{}/{}".format(WORKSPACE_STAGING_DIR, WORKERS_DIRNAME, name)
    quoted_dir = shlex.quote(worker_dir)
    quoted_target = shlex.quote(agent_id or name)
    report_step = (
        "r=$(sed -n 's/^finish_report_path:[[:space:]]*//p' {task_file} 2>/dev/null | head -n 1); "
        "printf '%s\\n' \"$r\"; "
        "printf '{exit_marker}\\n'; "
        'if [ -n "$r" ]; then mkdir -p {dir}/{reports} && '
        'cp -R {lead_dir}/"$(dirname "$r")"/. {dir}/{reports}/ 2>> {dir}/{stderr}; printf \'%s\\n\' "$?"; fi; '
    ).format(
        task_file=shlex.quote(posixpath.join(lead_work_dir, task_file)),
        exit_marker=_SECTION_MARKER.format("report_exit"),
        dir=quoted_dir,
        reports=WORKER_REPORTS_DIRNAME,
        lead_dir=shlex.quote(lead_work_dir),
        stderr=_WORKER_STDERR_FILENAME,
    )
    return (
        "mkdir -p {dir}; "
        "mngr transcript {target} --preserved --headless --format atif --output {dir}/{document} 2> {dir}/{stderr} "
        "|| mngr transcript {target} --headless --format atif --output {dir}/{document} 2>> {dir}/{stderr}; "
        "printf '{document_marker}\\n%s\\n' \"$?\"; "
        "mngr transcript {target} --preserved --headless --format jsonl > {dir}/{stream} 2>> {dir}/{stderr} "
        "|| mngr transcript {target} --headless --format jsonl > {dir}/{stream} 2>> {dir}/{stderr}; "
        "printf '{stream_marker}\\n%s\\n' \"$?\"; "
        "printf '{report_marker}\\n'; {report_step}"
        "printf '{stderr_marker}\\n'; tail -c {limit} {dir}/{stderr} 2>/dev/null; "
        "exit 0"
    ).format(
        dir=quoted_dir,
        target=quoted_target,
        document=WORKER_TRAJECTORY_FILENAME,
        stream=WORKER_STREAM_FILENAME,
        stderr=_WORKER_STDERR_FILENAME,
        document_marker=_SECTION_MARKER.format("document_exit"),
        stream_marker=_SECTION_MARKER.format("stream_exit"),
        report_marker=_SECTION_MARKER.format("report_path"),
        report_step=report_step if task_file else "",
        stderr_marker=_SECTION_MARKER.format("stderr"),
        limit=MAX_COMMAND_OUTPUT_BYTES,
    )


@pure
def _worker_state(raw_state: str) -> WorkerState:
    """How mngr's lifecycle states fold into what the capture cares about: a finished worker sits at
    its prompt (WAITING), is stopped, or is done, and either way its stream is complete."""
    try:
        state = AgentLifecycleState(raw_state.upper())
    except ValueError:
        logger.warning("The agent listing reported a lifecycle state mngr does not define: {!r}", raw_state)
        return WorkerState.UNKNOWN
    match state:
        case AgentLifecycleState.STOPPED | AgentLifecycleState.WAITING | AgentLifecycleState.DONE:
            return WorkerState.STOPPED
        case AgentLifecycleState.RUNNING | AgentLifecycleState.RUNNING_UNKNOWN_AGENT_TYPE:
            return WorkerState.RUNNING
        case AgentLifecycleState.REPLACED | AgentLifecycleState.UNKNOWN:
            return WorkerState.UNKNOWN
        case _ as unreachable:
            assert_never(unreachable)


@pure
def listing_reports_errors(listing_json: str) -> bool:
    """Whether `mngr list` reported that it could not see part of what it was asked for.

    It defaults to --on-error continue, so an unreachable provider yields the agents it did reach
    plus a non-empty `errors` array. The agents it named are still good; what it cannot support is
    a conclusion drawn from an agent's absence.
    """
    try:
        payload = json.loads(listing_json)
    except ValueError:
        return True
    if isinstance(payload, dict):
        return bool(payload.get("errors"))
    # A bare array is the other shape `parse_worker_listing` reads a listing in. Anything else
    # carries no agents at all, so it cannot speak for the agents it does not name either.
    return not isinstance(payload, list)


@pure
def parse_worker_listing(listing_json: str) -> tuple[WorkerListingEntry, ...]:
    """The agents out of `mngr list --format json`; empty when the listing could not be read."""
    try:
        payload = json.loads(listing_json)
    except ValueError as exc:
        logger.warning("The agent listing is not JSON: {}", exc)
        return ()
    raw_agents = payload.get("agents") if isinstance(payload, dict) else payload
    if not isinstance(raw_agents, list):
        logger.warning("The agent listing carries no agents array")
        return ()
    entries: list[WorkerListingEntry] = []
    for raw_agent in raw_agents:
        if not isinstance(raw_agent, dict) or not raw_agent.get("id"):
            continue
        entries.append(
            WorkerListingEntry(
                agent_id=str(raw_agent["id"]),
                name=str(raw_agent.get("name") or ""),
                agent_type=str(raw_agent.get("type") or ""),
                state=_worker_state(str(raw_agent.get("state") or "")),
                work_dir=str(raw_agent.get("work_dir") or ""),
            )
        )
    return tuple(entries)


@pure
def _worker_report_capture(task_file: str, report_path: str, copy_exit: str, detail: str) -> CapturedFile:
    """What the capture command said about the worker's report: the path the task file named (empty
    when it named none) and the copy's exit status, or why there was nothing to copy."""
    if not task_file:
        return _uncaptured(REASON_NOT_ATTEMPTED, "the launch named no task file")
    if not report_path:
        return _uncaptured(REASON_NO_REPORT_PATH, "")
    if copy_exit != "0":
        return _uncaptured(REASON_TRANSCRIPT_COMMAND_FAILED, detail)
    return CapturedFile(host_path=Path(WORKER_REPORTS_DIRNAME), failure_reason="", failure_detail="")


@pure
def _failed_worker_capture(
    launch: WorkerLaunch, entry: WorkerListingEntry | None, failure: CapturedFile
) -> WorkerCapture:
    """A worker whose capture never ran or never answered: every part records the same failure, and
    what is known about the worker is whatever the listing said."""
    return WorkerCapture(
        launch=launch,
        agent_id=entry.agent_id if entry is not None else "",
        agent_type=entry.agent_type if entry is not None else "",
        state=entry.state if entry is not None else WorkerState.UNKNOWN,
        document=failure,
        stream=failure,
        report=failure,
    )


@pure
def _state_of_unlisted_worker(is_listing_complete: bool, is_stream_captured: bool) -> WorkerState:
    """What a listing that does not hold a launched worker says about it.

    A complete listing that does not name the worker means no live agent answers to that name, so a
    stream the preservation-aware capture still produced came out of mngr's archive: the worker was
    destroyed after finishing. A listing that could not be read, or that mngr answered only in part,
    says nothing about an agent it does not name.
    """
    if is_listing_complete and is_stream_captured:
        return WorkerState.DESTROYED
    return WorkerState.UNKNOWN


def _worker_document_identity(document: CapturedFile, worker_name: str) -> tuple[str, str]:
    """The agent id and type a captured ATIF document names, both empty when no valid document
    reached the host. mngr builds the document's `agent.name` from the agent type it recorded for
    the worker, so a worker the listing never named still reports the type it actually ran."""
    if document.host_path is None:
        return "", ""
    try:
        parsed = parse_worker_document(document.host_path.read_text())
    except (OSError, TrajectoryDocumentError) as exc:
        logger.warning("Worker {}'s captured document names no identity: {}", worker_name, exc)
        return "", ""
    agent = parsed.get("agent")
    agent_type = str(agent.get("name") or "") if isinstance(agent, dict) else ""
    return str(parsed["trajectory_id"]), agent_type


@pure
def _is_anything_staged(captures: Sequence[WorkerCapture]) -> bool:
    """Whether any part of any capture was produced in the workspace, and so is there to transfer."""
    return any(
        part.host_path is not None
        for capture in captures
        for part in (capture.document, capture.stream, capture.report)
    )


@pure
def _uncaptured(failure_reason: str, failure_detail: str) -> CapturedFile:
    return CapturedFile(host_path=None, failure_reason=failure_reason, failure_detail=failure_detail)


@pure
def _transferred_capture(captured: CapturedFile, worker_dir: Path, transfer_failure_reason: str) -> CapturedFile:
    """A worker capture part with its staged name resolved to where the round's transfer put it under
    the worker's host directory, or the transfer's failure when it did not arrive."""
    if captured.host_path is None:
        return captured
    if transfer_failure_reason:
        return _uncaptured(transfer_failure_reason, "")
    host_path = worker_dir / captured.host_path
    if not host_path.exists():
        return _uncaptured(REASON_DOWNLOAD_FAILED, "")
    return CapturedFile(host_path=host_path, failure_reason="", failure_detail="")


def _launches_in_captured_streams(
    captures: Sequence[WorkerCapture], known_names: AbstractSet[str]
) -> list[WorkerLaunch]:
    """The workers the round's captured streams launched in turn, one per name not already known."""
    launches: list[WorkerLaunch] = []
    queued_names: set[str] = set()
    for capture in captures:
        if capture.stream.host_path is None:
            continue
        for launch in scan_worker_launches(
            parse_transcript_jsonl(capture.stream.host_path.read_text()),
            depth=capture.launch.depth + 1,
            lead_name=capture.launch.name,
        ):
            if launch.name in known_names or launch.name in queued_names:
                continue
            queued_names.add(launch.name)
            launches.append(launch)
    return launches


@pure
def not_attempted_transcript_capture() -> TranscriptCapture:
    """What a collector reports before the capture step has run: nothing was captured, and nothing
    failed either."""
    return TranscriptCapture(
        stream=_uncaptured(REASON_NOT_ATTEMPTED, ""),
        document=_uncaptured(REASON_NOT_ATTEMPTED, ""),
    )


@pure
def http_probe_command(url: str) -> str:
    """Fetch one URL from inside the workspace, reporting the status code, headers, timing, and a
    capped body head in a single round trip."""
    # No curl means the harness cannot ask, which is nothing like an app that refuses the
    # connection, so it is reported as its own section rather than as a status code. curl itself
    # still emits its -w line (with code 000) when it cannot connect, so no `||` fallback is needed
    # -- appending one would corrupt the status line.
    return (
        "if ! command -v curl > /dev/null 2>&1; then "
        "printf '{probe_error_marker}\\ncurl_missing\\n'; exit 0; fi; "
        "mkdir -p {staging}; "
        "code=$(curl -s -o {staging}/http_body -D {staging}/http_headers "
        "-w '%{{http_code}} %{{time_total}}' --max-time 20 {url}); "
        "printf '{status_marker}\\n%s\\n' \"$code\"; "
        "printf '{headers_marker}\\n'; cat {staging}/http_headers 2>/dev/null; "
        "printf '{body_marker}\\n'; head -c {body_limit} {staging}/http_body 2>/dev/null; "
        # The probe always exits 0 so that an app which refuses the connection reads as the
        # workspace falling short (status 000) rather than as the bridge failing. A real bridge
        # failure still surfaces, because it never gets as far as running this at all.
        "exit 0"
    ).format(
        staging=WORKSPACE_STAGING_DIR,
        url=shlex.quote(url),
        probe_error_marker=_SECTION_MARKER.format("probe_error"),
        status_marker=_SECTION_MARKER.format("status"),
        headers_marker=_SECTION_MARKER.format("headers"),
        body_marker=_SECTION_MARKER.format("body"),
        body_limit=MAX_HTTP_BODY_BYTES,
    )


@pure
def repo_state_command(repo_root: str, base_sha: str) -> str:
    """HEAD, working-tree cleanliness, and the agent's commits beyond the prepared clone, plus the
    incremental bundle that keeps a captured trial replayable in a fresh environment later."""
    return (
        "mkdir -p {staging}; cd {repo} || exit 97; "
        "printf '{head_marker}\\n'; git rev-parse HEAD 2>&1; "
        "printf '{status_marker}\\n'; git status --porcelain 2>&1; "
        "count=$(git rev-list --count {base}..HEAD 2>/dev/null || printf 'unknown'); "
        "printf '{count_marker}\\n%s\\n' \"$count\"; "
        "printf '{bundle_marker}\\n'; "
        # Pattern-matched rather than compared numerically: an empty or non-numeric count would make
        # `[ "$count" -gt 0 ]` itself an error under a POSIX shell.
        "case \"$count\" in \"\"|*[!0-9]*) printf 'no-commits' ;; 0) printf 'no-commits' ;; "
        "*) git bundle create {staging}/{bundle} {base}..HEAD 2>&1 ;; esac"
    ).format(
        staging=WORKSPACE_STAGING_DIR,
        repo=shlex.quote(repo_root),
        base=shlex.quote(base_sha),
        bundle=DELIVERABLE_BUNDLE_FILENAME,
        head_marker=_SECTION_MARKER.format("head_sha"),
        status_marker=_SECTION_MARKER.format("status"),
        count_marker=_SECTION_MARKER.format("commit_count"),
        bundle_marker=_SECTION_MARKER.format("bundle"),
    )


@pure
def test_command_wrapper(repo_root: str, command: str) -> str:
    """Run one declared test command in the delivered repo, reporting its exit code separately from
    its output so a command that prints nothing stays distinguishable from one that failed."""
    # The command runs in a SUBSHELL: a declared test command that ends in `exit` would otherwise
    # take the whole probe down with it, losing the exit code and the output it was asked to record.
    return (
        "mkdir -p {staging}; cd {repo} || exit 97; "
        "( {command} ) > {staging}/test_out 2>&1; rc=$?; "
        "printf '{exit_marker}\\n%s\\n' \"$rc\"; "
        "printf '{output_marker}\\n'; tail -c {limit} {staging}/test_out 2>/dev/null"
    ).format(
        repo=shlex.quote(repo_root),
        command=command,
        staging=WORKSPACE_STAGING_DIR,
        exit_marker=_SECTION_MARKER.format("exit_code"),
        output_marker=_SECTION_MARKER.format("output"),
        limit=MAX_COMMAND_OUTPUT_BYTES,
    )


@pure
def _timeout_or(reason: str, remaining_seconds: float) -> str:
    """A step that failed with the phase budget already gone timed out; anything else is the
    instrument. Recording which one it was is the difference between a diagnosable run and a shrug."""
    return REASON_TIMEOUT if remaining_seconds <= 0 else reason


@pure
def _test_command_status(is_bridge_success: bool, exit_code: str) -> CheckStatus:
    if not is_bridge_success:
        return CheckStatus.ERROR
    if exit_code == "0":
        return CheckStatus.PASSED
    return CheckStatus.FAILED


@pure
def parse_curl_status(status_section: str) -> tuple[int, float]:
    """curl's `%{http_code} %{time_total}` line; a transport failure reports code 0."""
    fields = status_section.split()
    status_code = int(fields[0]) if fields and fields[0].isdigit() else 0
    try:
        elapsed_seconds = float(fields[1]) if len(fields) > 1 else 0.0
    except ValueError:
        elapsed_seconds = 0.0
    return status_code, round(elapsed_seconds, 3)


@pure
def http_entry_status(
    is_bridge_success: bool, probe_error: str, status_code: int, body_head: str, check: HttpCheck
) -> tuple[CheckStatus, str]:
    """A probe that could not be taken is the harness's problem (ERROR); an app that answers the
    wrong thing -- or refuses the connection, which curl reports as status 0 -- is the workspace
    falling short (FAILED)."""
    if not is_bridge_success:
        return CheckStatus.ERROR, REASON_BRIDGE_FAILED
    if probe_error.strip():
        return CheckStatus.ERROR, REASON_PROBE_UNAVAILABLE
    if status_code != check.expect_status:
        return CheckStatus.FAILED, REASON_WRONG_STATUS
    if check.expect_body_regex and re.search(check.expect_body_regex, body_head) is None:
        return CheckStatus.FAILED, REASON_BODY_MISMATCH
    return CheckStatus.PASSED, ""


@pure
def resolve_http_targets(check: HttpCheck, delivered_apps: Sequence[RegisteredApp]) -> tuple[RegisteredApp, ...]:
    """Which apps a check probes. The fan-out target covers the DELIVERED apps, so an abandoned
    throwaway's dead port is never probed as though it were the deliverable."""
    if check.target == REGISTERED_APPS_HTTP_TARGET:
        return tuple(app for app in delivered_apps if app.url)
    return tuple(app for app in delivered_apps if app.name == check.target and app.url)


@pure
def registration_entry(
    check_id: str,
    min_registered_apps: int,
    # None when the delivered set could not be resolved at all, which is not the same claim as an
    # empty one: a workspace that registered nothing is the agent shipping nothing, and must score
    # against it.
    delivered_apps: Sequence[RegisteredApp] | None,
    unresolved_reason: str,
) -> ManifestEntry:
    """One `min_registered_apps` verdict. `unresolved_reason` says why the delivered set is None,
    and is non-empty exactly when it is."""
    if delivered_apps is None:
        assert unresolved_reason, "an unresolved delivered set must name the reason it is unresolved"
        return _entry(check_id, CheckClass.APP, CheckStatus.ERROR, unresolved_reason, "", "")
    assert not unresolved_reason, "a resolved delivered set cannot also carry an unresolved reason"
    is_met = len(delivered_apps) >= min_registered_apps
    return _entry(
        check_id,
        CheckClass.APP,
        CheckStatus.PASSED if is_met else CheckStatus.FAILED,
        "" if is_met else REASON_TOO_FEW_APPS,
        "{} delivered app(s) registered ({}); expected at least {}".format(
            len(delivered_apps),
            ", ".join(app.name for app in delivered_apps) or "none",
            min_registered_apps,
        ),
        "",
    )


@pure
def _service_state_for(program_name: str, service_state_by_name: Mapping[str, str]) -> str:
    """The reported state of one supervisord program. supervisorctl prints a grouped program as
    ``group:process``, so a bare-name lookup alone would read a grouped service as absent."""
    if program_name in service_state_by_name:
        return service_state_by_name[program_name]
    for reported_name, state in service_state_by_name.items():
        if reported_name.rpartition(":")[2] == program_name:
            return state
    return "ABSENT"


@pure
def service_entries(
    check_id: str,
    delivered_apps: Sequence[RegisteredApp],
    service_state_by_name: Mapping[str, str],
    program_by_registration: Mapping[str, str],
    is_services_readable: bool,
    is_supervisord_conf_readable: bool,
) -> tuple[ManifestEntry, ...]:
    """One entry per delivered app: an app whose supervising program is not running is exactly the
    "started it, then it crashed" failure the liveness checks exist to catch.

    The registry row is mapped to its program through the config's `forward_port.py` invocations,
    not by assuming the two share a name -- a multi-port app registers extra origin-label rows that
    no program owns. A row no program registers at all is a real shortfall of the minds-app contract
    (the app was started by hand and would not survive a restart), recorded under its own reason so
    it stays distinguishable from a program that exists and crashed -- and, when the config could
    not be read whole, from a row whose program the capture simply could not see.
    """
    entries: list[ManifestEntry] = []
    for app in delivered_apps:
        entry_id = "{}_service_{}".format(check_id, slugify(app.name))
        if not is_services_readable:
            entries.append(_entry(entry_id, CheckClass.APP, CheckStatus.ERROR, REASON_SERVICES_UNREADABLE, "", ""))
            continue
        # The config join first (it survives renames and multi-port rows), then a program named
        # exactly like the row -- unambiguous, and it covers a service that registers its port at
        # runtime instead of through a forward_port call in the config.
        program_name = program_by_registration.get(app.name)
        if program_name is None and _service_state_for(app.name, service_state_by_name) != "ABSENT":
            program_name = app.name
        if program_name is None:
            if is_supervisord_conf_readable:
                status = CheckStatus.FAILED
                reason = REASON_NO_SUPERVISED_PROGRAM
                detail = "no supervisord program registers {}, so nothing supervises it".format(app.name)
            else:
                status = CheckStatus.ERROR
                reason = REASON_SUPERVISORD_CONF_UNREADABLE
                detail = (
                    "supervisord is running programs the captured config does not declare, so "
                    "which program owns {} could not be read".format(app.name)
                )
            entries.append(_entry(entry_id, CheckClass.APP, status, reason, detail, ""))
            continue
        state = _service_state_for(program_name, service_state_by_name)
        is_running = state == "RUNNING"
        entries.append(
            _entry(
                entry_id,
                CheckClass.APP,
                CheckStatus.PASSED if is_running else CheckStatus.FAILED,
                "" if is_running else REASON_SERVICE_NOT_RUNNING,
                "supervisord reports {} (serving {}) as {}".format(program_name, app.name, state),
                "",
            )
        )
    return tuple(entries)


class EvidenceCollector(MutableModel):
    """Runs the collection phase against a live workspace and writes the evidence bundle.

    It is a class only because every step shares one bridge target, one budget, and one accumulating
    record; all of the decision logic lives in the pure functions above.
    """

    model_config = ConfigDict(frozen=False, extra="forbid", arbitrary_types_allowed=True)

    environment: BaseEnvironment = Field(frozen=True, description="The harbor environment (the box)")
    box_env: dict[str, str] = Field(frozen=True, description="The per-trial env every bridge exec runs with")
    workspace_agent_id: str = Field(frozen=True, description="The nested workspace the evidence is collected from")
    chat_agent_id: str = Field(
        frozen=True,
        description="The workspace's chat agent, whose common transcript is the trial's transcript; empty when "
        "the driver never resolved one, in which case the capture is not attempted",
    )
    case: CaseConfig = Field(frozen=True, description="The case whose expanded expectations drive the probes")
    # The conversation the driver just held, as the records state.json publishes. Only the timing
    # class reads them, and only to turn "the client said it was satisfied" into a span; they are
    # passed in rather than re-read from the state file so the collector never has to parse what
    # the driver is still writing.
    entry_records: tuple[EntryRecord, ...] = Field(
        frozen=True, default=(), description="How each prompts entry played out, in order"
    )
    turn_records: tuple[TurnRecord, ...] = Field(
        frozen=True, default=(), description="Every answered client message, in order"
    )
    clone_base_sha: str = Field(frozen=True, description="HEAD of the prepared dwt clone; the git bundle's base")
    dwt_tip_sha: str = Field(frozen=True, description="The dwt tip the base clone was made from")
    # Required rather than defaulted to None: an unmeasured trial must be a deliberate claim, never
    # the result of a caller leaving the field out.
    preexisting_registrations: frozenset[str] | None = Field(
        frozen=True, description="Registry names the workspace already served before the agent ran"
    )
    host_logs_dir: Path = Field(frozen=True, description="The trial's host-side logs dir")
    deadline: float = Field(frozen=True, description="Monotonic-clock deadline for the whole phase")
    # None when no key was available to build one; the flows are then recorded as unmeasurable
    # rather than silently skipped.
    verification_agent: ui_flows.VerificationAgent | None = Field(
        frozen=True, default=None, description="Decides each flow's next action and reads its final state"
    )
    verifier_model: str = Field(frozen=True, default="", description="Model the UI-flow agent reasons with")
    # Minted per trial. The driver owns the forward instance precisely so it knows this, rather
    # than having to discover a cookie the minds backend minted for itself.
    preauth_cookie: SecretStr = Field(
        frozen=True, default=SecretStr(""), description="Pre-arms the forward proxy's session for the browser"
    )
    browser_bridge_token: SecretStr = Field(
        frozen=True, default=SecretStr(""), description="The forward proxy's plain-browser bridge token"
    )
    # Set for the duration of one flow. Every bridge call inside it is clamped to this as well as
    # to the phase deadline, or a single stuck fleet command could overrun the flow's budget many
    # times over before the loop's own check noticed.
    flow_deadline: float = Field(default=0.0, description="Monotonic deadline for the flow being driven")
    # How long the readiness loops wait between polls. A field so a test can drive the loops to
    # their give-up condition without actually sleeping through them.
    readiness_poll_seconds: float = Field(
        default=_FORWARD_READY_POLL_SECONDS, description="Seconds between readiness polls"
    )
    entries: list[ManifestEntry] = Field(default_factory=list, description="Recorded probes, in collection order")
    phases: list[PhaseTiming] = Field(default_factory=list, description="Wall-clock spent per collection phase")
    trace: list[TraceRecord] = Field(default_factory=list, description="Every command the collector ran")
    repo_root: str = Field(default="", description="The delivered repo's path in the workspace, once discovered")
    registry_text: str = Field(default="", description="The app registry exactly as captured")
    is_registry_present: bool = Field(default=False, description="Whether the registry file exists at all")
    services_text: str = Field(default="", description="supervisorctl status output exactly as captured")
    supervisord_conf: str = Field(
        default="", description="The workspace's supervisord config, and its drop-ins, as captured"
    )
    isolated_instance_services: frozenset[str] = Field(
        default=frozenset(), description="Registry rows owned by throwaway preview servers"
    )
    # None until the registry has been read, and again if it turned out to be unreadable.
    registered_apps: tuple[RegisteredApp, ...] | None = Field(default=None, description="The parsed registry")
    serving_app_names: set[str] = Field(
        default_factory=set, description="Delivered apps that answered their root-path probe as expected"
    )
    transcript_capture: TranscriptCapture = Field(
        default_factory=not_attempted_transcript_capture,
        description="What the capture step brought out of the workspace's common transcript",
    )
    worker_captures: list[WorkerCapture] = Field(
        default_factory=list,
        description="One record per background worker launched in the trial: the chat agent's and, in turn, theirs",
    )
    worker_capture_overflow: list[str] = Field(
        default_factory=list, description="Launched worker names the count or depth caps left uncaptured"
    )
    started_at: str = Field(default="", description="UTC ISO timestamp the phase began")

    @property
    def _host_dir(self) -> Path:
        return self.host_logs_dir / VERIFICATION_DIRNAME

    @property
    def _box_dir(self) -> str:
        return box_verification_dir()

    @property
    def _unresolved_reason(self) -> str:
        """Why the delivered set cannot be resolved, or empty when it can.

        Two distinct instrument failures land here, and the manifest keeps them apart: an app
        registry that is absent or unreadable at collection time, and a pre-existing set the driver
        could not determine before the first turn. Without the latter there is no way to tell what
        the agent added from what booted with the workspace, so the answer is "unmeasured", never
        "everything counts".

        Non-empty exactly when ``registered_apps`` is None, which is what lets every caller decide
        from the one it has to hand.
        """
        if self.preexisting_registrations is None:
            return REASON_PREEXISTING_UNKNOWN
        if self.registered_apps is None:
            return REASON_REGISTRY_UNREADABLE if self.is_registry_present else REASON_REGISTRY_ABSENT
        return ""

    @property
    def _delivered_apps(self) -> tuple[RegisteredApp, ...] | None:
        """The registry rows that count as the case's deliverable, or None if the set could not be
        resolved. Both the app checks and the HTTP fan-out score exactly this set."""
        if self.registered_apps is None:
            return None
        return resolve_delivered_apps(self.registered_apps, self.isolated_instance_services)

    @property
    def _remaining_seconds(self) -> float:
        return self.deadline - time.monotonic()

    def _budget(self, wanted_seconds: int) -> int:
        """A step's bridge timeout, clamped to what is left of the phase deadline."""
        return max(1, min(wanted_seconds, int(self._remaining_seconds)))

    async def _run_in_workspace(self, phase: str, command: str, wanted_seconds: int) -> tuple[bool, str]:
        is_success, output = await minds_bridge.run_in_workspace(
            self.environment, self.box_env, self.workspace_agent_id, command, self._budget(wanted_seconds)
        )
        self.trace.append(
            TraceRecord(
                timestamp=ui_flows.utc_now_iso(),
                phase=phase,
                command=command,
                is_success=is_success,
                output=ui_flows.bounded_tail(output, MAX_TRACE_OUTPUT_CHARS),
            )
        )
        return is_success, output

    async def _pull_staged_path(self, phase: str, relative_path: str, is_directory: bool) -> bool:
        """Pull one workspace-staged path into the box's evidence directory over the snapshot
        transport, under the same relative name. Rsyncing a named file INTO the directory keeps its
        basename; a directory is sent as its contents into a directory of that name (both trailing
        slashes are rsync's "contents of")."""
        source = "{}/{}{}".format(WORKSPACE_STAGING_DIR, relative_path, "/" if is_directory else "")
        destination = "{}/{}".format(self._box_dir, "{}/".format(relative_path) if is_directory else "")
        command = "cd {mngr} && uv run mngr rsync {agent}:{src} {dest}".format(
            mngr=minds_bridge.BOX_MNGR_DIR,
            agent=shlex.quote(self.workspace_agent_id),
            src=source,
            dest=destination,
        )
        result = await minds_bridge.run_in_box(
            self.environment, command, self.box_env, self._budget(_RSYNC_TIMEOUT_SECONDS)
        )
        is_success = result.return_code == 0
        self.trace.append(
            TraceRecord(
                timestamp=ui_flows.utc_now_iso(),
                phase=phase,
                command=command,
                is_success=is_success,
                output=ui_flows.bounded_tail((result.stdout or "") + (result.stderr or ""), MAX_TRACE_OUTPUT_CHARS),
            )
        )
        return is_success

    async def _write_evidence(self, relative_name: str, content: str) -> None:
        """Write one evidence file host-side and mirror it into the box, where the task's declared
        artifact directory picks the whole bundle up for the verifier."""
        host_path = self._host_dir / relative_name
        host_path.parent.mkdir(parents=True, exist_ok=True)
        host_path.write_text(content)
        await self.environment.upload_file(host_path, "{}/{}".format(self._box_dir, relative_name))

    async def _download_evidence_dir(self, relative_dir: str) -> bool:
        """Copy one directory already in the box's evidence directory to the host-side bundle."""
        host_dir = self._host_dir / relative_dir
        host_dir.parent.mkdir(parents=True, exist_ok=True)
        try:
            await self.environment.download_dir("{}/{}".format(self._box_dir, relative_dir), host_dir)
        except (OSError, RuntimeError, ModalError) as exc:
            logger.warning("Could not download {} from the box: {}", relative_dir, exc)
            return False
        return True

    async def _download_evidence(self, relative_name: str) -> Path | None:
        """Copy one file already in the box's evidence directory to the host-side bundle: the reverse of
        `_write_evidence`, for evidence that was pulled out of the workspace rather than written here.
        A filesystem transfer rather than an exec, so the file's size never has to fit through a
        command's stdout. None when the transfer failed."""
        host_path = self._host_dir / relative_name
        host_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            await self.environment.download_file("{}/{}".format(self._box_dir, relative_name), host_path)
        except (OSError, RuntimeError, ModalError) as exc:
            logger.warning("Could not download {} from the box: {}", relative_name, exc)
            return None
        return host_path

    async def _flush_record(self) -> None:
        """Rewrite the manifest and trace so a crash after any step still leaves a readable record."""
        await self._write_evidence(TRACE_FILENAME, self._trace_jsonl())
        await self._write_evidence(MANIFEST_FILENAME, json.dumps(self.manifest().model_dump(mode="json"), indent=2))

    def _trace_jsonl(self) -> str:
        lines = [json.dumps(record.model_dump(mode="json")) for record in self.trace]
        return "\n".join(lines) + ("\n" if lines else "")

    def manifest(self) -> EvidenceManifest:
        return EvidenceManifest(
            schema_version=MANIFEST_SCHEMA_VERSION,
            case_id=self.case.case_id,
            base_sha=self.clone_base_sha,
            dwt_tip_sha=self.dwt_tip_sha,
            preexisting_registrations=(
                None if self.preexisting_registrations is None else tuple(sorted(self.preexisting_registrations))
            ),
            is_expectations_declared=self.case.expectations is not None,
            is_evidence_complete=all(entry.status != CheckStatus.ERROR for entry in self.entries),
            started_at=self.started_at or ui_flows.utc_now_iso(),
            phases=tuple(self.phases),
            entries=tuple(self.entries),
        )

    def _record_phase(self, name: str, started_at: float) -> None:
        self.phases.append(PhaseTiming(name=name, seconds=round(time.monotonic() - started_at, 2)))

    async def collect(self, is_expectations_collection_wanted: bool) -> EvidenceManifest:
        """Run the collection phase.

        The always-on capture runs for every trial, including cases with no expectations at all. The
        expectations-driven steps are skipped on trials that never finished: their structural gates
        already zero the reward, so probing an unfinished build buys nothing.
        """
        self.started_at = ui_flows.utc_now_iso()
        # Idempotent: setup already created this so the declared artifact always exists, but a
        # collector run against a box that skipped setup must not write into a missing directory.
        await ensure_evidence_dir(self.environment)
        await self._capture_workspace_state()
        # Before the inventory: the transcript is the cheapest and most valuable capture, and the
        # home-tree walk is the slowest of the always-on ones.
        if self.chat_agent_id:
            await self._capture_common_transcript()
            await self._capture_workers()
        await self._capture_file_inventory()
        expectations = self.case.expectations
        if is_expectations_collection_wanted and expectations is not None:
            # Reads what the always-on capture already brought out, so it costs no round trip and is
            # recorded even when the probes below run the budget down.
            self._evaluate_process_checks(expectations)
            if expectations.is_deliverable_bundle_required:
                await self._capture_repo_state()
            await self._run_test_commands(expectations)
            await self._run_http_probes(expectations)
            self._evaluate_app_checks(expectations)
            # Last, and after the HTTP probes: driving the UI is the most expensive step and the
            # one most likely to exhaust the budget, and everything above is worth having anyway.
            await self._run_ui_flows(expectations)
            # After every other class, because a timing prerequisite is answered from the failures
            # this bundle already records.
            self._evaluate_timing_checks(expectations)
        await self._flush_record()
        return self.manifest()

    def verifier_usage(self) -> ui_flows.VerifierUsage:
        """What the UI-flow agent spent. Harness spend, reported beside the decider's."""
        calls = tuple(self.verification_agent.calls) if self.verification_agent is not None else ()
        return ui_flows.summarize_verifier_usage(calls, self.verifier_model)

    async def _capture_workspace_state(self) -> None:
        """Always-on: the app registry and supervisord's view of the world. Cheap enough that even a
        case with no expectations gets it, which is what makes a ships-nothing trial diagnosable."""
        started_at = time.monotonic()
        is_success, output = await self._run_in_workspace(
            "workspace_state", workspace_state_command(), PROBE_TIMEOUT_SECONDS
        )
        sections = split_sections(output)
        self.repo_root = sections.get("repo_root", "").strip()
        self.is_registry_present = is_registry_status_present(sections)
        self.registry_text = sections.get("registry", "")
        self.services_text = sections.get("services", "")
        self.supervisord_conf = sections.get("supervisord", "")
        self.isolated_instance_services = parse_isolated_instance_services(sections.get("isolated_instances", ""))
        # A row can only be stamped pre-existing-or-not against a known pre-existing set, so an
        # unknown one leaves the registry unresolved even though it is captured verbatim just below.
        preexisting = self.preexisting_registrations
        self.registered_apps = (
            parse_apps_registry(self.registry_text, preexisting)
            if self.is_registry_present and preexisting is not None
            else None
        )
        await self._write_evidence(APPS_REGISTRY_FILENAME, self.registry_text)
        await self._write_evidence(SERVICES_FILENAME, self.services_text)
        if not is_success:
            self.entries.append(
                _entry(
                    "workspace_state",
                    CheckClass.APP,
                    CheckStatus.ERROR,
                    _timeout_or(REASON_BRIDGE_FAILED, self._remaining_seconds),
                    ui_flows.bounded_tail(output, MAX_COMMAND_OUTPUT_CHARS),
                    "",
                )
            )
        self._record_phase("workspace_state", started_at)
        await self._flush_record()

    async def _capture_common_transcript(self) -> None:
        """Always-on: the chat agent's common transcript, as the raw stream and as the ATIF document
        mngr builds from it.

        This is the trial's transcript record rather than outcome evidence, so it feeds the driver's
        trial metadata instead of the manifest: a manifest error here would tell the outcome judge a
        check went unmeasured, over a fact about the transcript that has nothing to do with the
        deliverable. Each half is brought out on its own, so a document that fails to build still
        leaves the stream.
        """
        started_at = time.monotonic()
        is_success, output = await self._run_in_workspace(
            "common_transcript", transcript_capture_command(self.chat_agent_id), _TRANSCRIPT_TIMEOUT_SECONDS
        )
        sections = split_sections(output)
        # The commands' own stderr is the diagnostic when they ran; the bridge's output when they
        # did not.
        detail = ui_flows.bounded_tail(sections.get("stderr", "") if is_success else output, MAX_COMMAND_OUTPUT_CHARS)
        stream = await self._bring_out_transcript_file(
            COMMON_TRANSCRIPT_FILENAME, is_success, sections.get("stream_exit", "").strip(), detail
        )
        document = await self._bring_out_transcript_file(
            WORKSPACE_TRAJECTORY_FILENAME, is_success, sections.get("document_exit", "").strip(), detail
        )
        self.transcript_capture = TranscriptCapture(stream=stream, document=document)
        self._record_phase("common_transcript", started_at)
        await self._flush_record()

    async def _bring_out_transcript_file(
        self, filename: str, is_bridge_success: bool, exit_code: str, detail: str
    ) -> CapturedFile:
        """One staged transcript file's journey: workspace staging -> box bundle -> host bundle, or
        the first step that stopped it."""
        if not is_bridge_success:
            return _uncaptured(_timeout_or(REASON_BRIDGE_FAILED, self._remaining_seconds), detail)
        if exit_code != "0":
            return _uncaptured(REASON_TRANSCRIPT_COMMAND_FAILED, detail)
        if not await self._pull_staged_path("common_transcript", filename, is_directory=False):
            return _uncaptured(_timeout_or(REASON_PULL_FAILED, self._remaining_seconds), "")
        host_path = await self._download_evidence(filename)
        if host_path is None:
            return _uncaptured(REASON_DOWNLOAD_FAILED, "")
        return CapturedFile(host_path=host_path, failure_reason="", failure_detail="")

    async def _capture_workers(self) -> None:
        """Always-on: the background workers the chat agent launched, discovered from its captured stream,
        and the workers those launched in turn, followed for MAX_WORKER_ROUNDS rounds.

        Each round captures the workers the previous round's streams launched, then pulls and downloads
        the whole workers directory so the next round can scan them host-side. Like the transcript
        capture this is the trial's record, not outcome evidence: failures land on the driver's metadata,
        never in the manifest.
        """
        stream_path = self.transcript_capture.stream.host_path
        if stream_path is None:
            return
        pending = scan_worker_launches(parse_transcript_jsonl(stream_path.read_text()), depth=0, lead_name="")
        if not pending:
            return
        started_at = time.monotonic()
        listing = await self._capture_worker_listing()
        work_dir_by_name = {entry.name: entry.work_dir for entry in listing.entries}
        chat_entry = next((entry for entry in listing.entries if entry.agent_id == self.chat_agent_id), None)
        work_dir_by_name[""] = chat_entry.work_dir if chat_entry is not None else DEFAULT_WORKSPACE_REPO_ROOT
        # Every name already captured or already counted as overflow, so a name two leads both
        # launch, or one the cap already dropped, is recorded once.
        known_names: set[str] = set()
        for _round in range(MAX_WORKER_ROUNDS):
            if not pending:
                break
            this_round: list[WorkerCapture] = []
            for launch in pending:
                known_names.add(launch.name)
                if len(self.worker_captures) + len(this_round) >= MAX_WORKER_COUNT:
                    self.worker_capture_overflow.append(launch.name)
                    continue
                this_round.append(
                    await self._capture_one_worker(launch, listing, work_dir_by_name.get(launch.lead_name, ""))
                )
            if not this_round:
                pending = []
                break
            # The whole directory in one rsync and one download, when the round staged anything (a
            # round whose captures were all skipped for the budget or failed at the bridge has nothing
            # to bring over); a failure of either is recorded on every part the round captured.
            is_staged = _is_anything_staged(this_round)
            if is_staged and not await self._pull_staged_path("workers", WORKERS_DIRNAME, is_directory=True):
                transfer_failure_reason = _timeout_or(REASON_PULL_FAILED, self._remaining_seconds)
            elif is_staged and not await self._download_evidence_dir(WORKERS_DIRNAME):
                transfer_failure_reason = REASON_DOWNLOAD_FAILED
            else:
                transfer_failure_reason = ""
            settled = self._settled_worker_captures(this_round, transfer_failure_reason)
            self.worker_captures.extend(settled)
            pending = _launches_in_captured_streams(settled, known_names)
        self.worker_capture_overflow.extend(launch.name for launch in pending)
        self._record_phase("workers", started_at)
        await self._flush_record()

    async def _capture_worker_listing(self) -> WorkerListing:
        """The workspace's agents, or nothing when the listing could not be had: the workers are then
        captured by name alone, with no id, state, or lead work dir from the listing."""
        is_success, output = await self._run_in_workspace("workers", worker_listing_command(), _WORKER_TIMEOUT_SECONDS)
        if not is_success:
            logger.warning(
                "Could not list the workspace's agents: {}", ui_flows.bounded_tail(output, MAX_COMMAND_OUTPUT_CHARS)
            )
            return WorkerListing()
        sections = split_sections(output)
        listing_json = sections.get("listing", "")
        entries = parse_worker_listing(listing_json)
        list_exit = sections.get("list_exit", "").strip()
        is_complete = list_exit == "0" and not listing_reports_errors(listing_json)
        if not entries or not is_complete:
            logger.warning(
                "The workspace's agent listing named {} agent(s) and is {} (exit {}): {}",
                len(entries),
                "complete" if is_complete else "incomplete",
                list_exit,
                ui_flows.bounded_tail(sections.get("stderr", ""), MAX_COMMAND_OUTPUT_CHARS),
            )
        return WorkerListing(entries=entries, is_complete=is_complete)

    async def _capture_one_worker(
        self, launch: WorkerLaunch, listing: WorkerListing, lead_work_dir: str
    ) -> WorkerCapture:
        """Run one worker's capture and record what the workspace said about each part; the host paths
        are filled in once the round's directory transfer has landed."""
        entry = next((entry for entry in listing.entries if entry.name == launch.name), None)
        if self._remaining_seconds <= 0:
            return _failed_worker_capture(launch, entry, _uncaptured(REASON_TIMEOUT, ""))
        is_success, output = await self._run_in_workspace(
            "workers",
            worker_capture_command(
                launch.name, entry.agent_id if entry is not None else "", lead_work_dir, launch.task_file
            ),
            _WORKER_TIMEOUT_SECONDS,
        )
        sections = split_sections(output)
        detail = ui_flows.bounded_tail(sections.get("stderr", "") if is_success else output, MAX_COMMAND_OUTPUT_CHARS)
        if not is_success:
            return _failed_worker_capture(
                launch, entry, _uncaptured(_timeout_or(REASON_BRIDGE_FAILED, self._remaining_seconds), detail)
            )
        is_stream_captured = sections.get("stream_exit", "").strip() == "0"
        return WorkerCapture(
            launch=launch,
            agent_id=entry.agent_id if entry is not None else "",
            agent_type=entry.agent_type if entry is not None else "",
            state=(
                entry.state
                if entry is not None
                else _state_of_unlisted_worker(listing.is_complete, is_stream_captured)
            ),
            # Captured here means "the workspace produced it"; the host path is attached after the
            # transfer, which is when the file is actually in hand.
            document=(
                CapturedFile(host_path=Path(WORKER_TRAJECTORY_FILENAME), failure_reason="", failure_detail="")
                if sections.get("document_exit", "").strip() == "0"
                else _uncaptured(REASON_TRANSCRIPT_COMMAND_FAILED, detail)
            ),
            stream=(
                CapturedFile(host_path=Path(WORKER_STREAM_FILENAME), failure_reason="", failure_detail="")
                if is_stream_captured
                else _uncaptured(REASON_TRANSCRIPT_COMMAND_FAILED, detail)
            ),
            report=_worker_report_capture(
                launch.task_file,
                sections.get("report_path", "").strip(),
                sections.get("report_exit", "").strip(),
                detail,
            ),
        )

    def _settled_worker_captures(
        self, captures: Sequence[WorkerCapture], transfer_failure_reason: str
    ) -> list[WorkerCapture]:
        """The round's captures with their host paths resolved against what the transfer brought over."""
        settled: list[WorkerCapture] = []
        for capture in captures:
            worker_dir = self._host_dir / WORKERS_DIRNAME / capture.launch.name
            document = _transferred_capture(capture.document, worker_dir, transfer_failure_reason)
            document_id, document_type = (
                _worker_document_identity(document, capture.launch.name)
                if not capture.agent_id or not capture.agent_type
                else ("", "")
            )
            settled.append(
                WorkerCapture(
                    launch=capture.launch,
                    agent_id=capture.agent_id or document_id,
                    agent_type=capture.agent_type or document_type,
                    state=capture.state,
                    document=document,
                    stream=_transferred_capture(capture.stream, worker_dir, transfer_failure_reason),
                    report=_transferred_capture(capture.report, worker_dir, transfer_failure_reason),
                )
            )
        return settled

    async def _capture_file_inventory(self) -> None:
        started_at = time.monotonic()
        is_success, output = await self._run_in_workspace(
            "file_inventory", file_inventory_command(), _INVENTORY_TIMEOUT_SECONDS
        )
        is_pulled = (
            await self._pull_staged_path("file_inventory", FILE_INVENTORY_FILENAME, is_directory=False)
            if is_success
            else False
        )
        self.entries.append(
            _entry(
                "file_inventory",
                CheckClass.FILES,
                CheckStatus.PASSED if is_pulled else CheckStatus.ERROR,
                "" if is_pulled else _timeout_or(REASON_BRIDGE_FAILED, self._remaining_seconds),
                "{} file(s) inventoried".format(output.strip())
                if is_pulled
                else ui_flows.bounded_tail(output, MAX_COMMAND_OUTPUT_CHARS),
                "{}/{}".format(VERIFICATION_DIRNAME, FILE_INVENTORY_FILENAME),
            )
        )
        self._record_phase("file_inventory", started_at)
        await self._flush_record()

    async def _capture_repo_state(self) -> None:
        """The delivered repo's committed state: HEAD, working-tree cleanliness, and an incremental
        bundle against the prepared clone. Deferred fresh-environment verification can replay a
        captured trial's deliverable from this without the trial paying for a second workspace."""
        started_at = time.monotonic()
        if not self.repo_root:
            self.entries.append(
                _entry("deliverable_bundle", CheckClass.BUNDLE, CheckStatus.ERROR, REASON_REPO_NOT_FOUND, "", "")
            )
            self._record_phase("repo_state", started_at)
            await self._flush_record()
            return
        is_success, output = await self._run_in_workspace(
            "repo_state", repo_state_command(self.repo_root, self.clone_base_sha), _BUNDLE_TIMEOUT_SECONDS
        )
        sections = split_sections(output)
        head_sha = sections.get("head_sha", "").strip()
        porcelain = sections.get("status", "")
        commit_count = sections.get("commit_count", "").strip()
        is_bundle_expected = commit_count.isdigit() and int(commit_count) > 0
        is_bundle_pulled = (
            await self._pull_staged_path("repo_state", DELIVERABLE_BUNDLE_FILENAME, is_directory=False)
            if is_success and is_bundle_expected
            else False
        )
        await self._write_evidence(
            REPO_STATE_FILENAME,
            json.dumps(
                {
                    "repo_root": self.repo_root,
                    # Both SHAs travel: a replay regenerates the base clone from the dwt tip, checks
                    # it reproduces base_sha, then unbundles the agent's commits onto it.
                    "base_sha": self.clone_base_sha,
                    "dwt_tip_sha": self.dwt_tip_sha,
                    "head_sha": head_sha,
                    "commit_count_beyond_base": commit_count,
                    "is_clean": not porcelain.strip(),
                    "status_porcelain": ui_flows.bounded_tail(porcelain, MAX_COMMAND_OUTPUT_CHARS),
                },
                indent=2,
            ),
        )
        is_captured = is_success and bool(head_sha) and (is_bundle_pulled or not is_bundle_expected)
        self.entries.append(
            _entry(
                "deliverable_bundle",
                CheckClass.BUNDLE,
                CheckStatus.PASSED if is_captured else CheckStatus.ERROR,
                "" if is_captured else _timeout_or(REASON_BRIDGE_FAILED, self._remaining_seconds),
                "HEAD {} ({} commit(s) beyond the prepared clone); {}".format(
                    head_sha[:12] or "unknown",
                    commit_count or "unknown",
                    "clean" if not porcelain.strip() else "dirty working tree",
                ),
                "{}/{}".format(VERIFICATION_DIRNAME, DELIVERABLE_BUNDLE_FILENAME) if is_bundle_pulled else "",
            )
        )
        self._record_phase("repo_state", started_at)
        await self._flush_record()

    async def _run_test_commands(self, expectations: ExpandedExpectations) -> None:
        """The agent's own tests, if the case declared any. Recorded and judge-visible but never
        gated: gating here would punish cases whose prompts never mentioned tests, and reward an
        agent that writes one trivial assert."""
        if not expectations.test_commands:
            return
        started_at = time.monotonic()
        for index, command in enumerate(expectations.test_commands):
            entry_id = "test_command_{}".format(index)
            if not self.repo_root:
                self.entries.append(
                    _entry(entry_id, CheckClass.TEST_COMMAND, CheckStatus.ERROR, REASON_REPO_NOT_FOUND, command, "")
                )
                continue
            if self._remaining_seconds <= 0:
                self.entries.append(
                    _entry(entry_id, CheckClass.TEST_COMMAND, CheckStatus.ERROR, REASON_TIMEOUT, command, "")
                )
                continue
            is_success, output = await self._run_in_workspace(
                "test_commands", test_command_wrapper(self.repo_root, command), _TEST_COMMAND_TIMEOUT_SECONDS
            )
            sections = split_sections(output)
            exit_code = sections.get("exit_code", "").strip()
            self.entries.append(
                _entry(
                    entry_id,
                    CheckClass.TEST_COMMAND,
                    _test_command_status(is_success, exit_code),
                    "" if exit_code == "0" else (REASON_NONZERO_EXIT if is_success else REASON_BRIDGE_FAILED),
                    "$ {}\nexit {}\n{}".format(
                        command,
                        exit_code or "unknown",
                        ui_flows.bounded_tail(sections.get("output", ""), MAX_COMMAND_OUTPUT_CHARS),
                    ),
                    "",
                )
            )
        self._record_phase("test_commands", started_at)
        await self._flush_record()

    async def _run_http_probes(self, expectations: ExpandedExpectations) -> None:
        """Probe the app AS DELIVERED. The harness never starts it: Minds' promise to the client is a
        running app tab, so "built it but never started it" must read as a delivery failure rather
        than get silently repaired here."""
        if not expectations.http_checks:
            return
        started_at = time.monotonic()
        for check in expectations.http_checks:
            await self._run_one_http_check(check)
        self._record_phase("http_probes", started_at)
        await self._flush_record()

    async def _run_one_http_check(self, check: HttpCheck) -> None:
        delivered_apps = self._delivered_apps
        if delivered_apps is None:
            # Without a resolved delivered set there is no address to probe; that is the harness
            # failing to measure, not the app refusing a connection.
            self.entries.append(
                _entry(
                    check.check_id,
                    CheckClass.HTTP,
                    CheckStatus.ERROR,
                    self._unresolved_reason,
                    "the delivered apps could not be resolved, so target {!r} has no address".format(check.target),
                    "",
                )
            )
            return
        targets = resolve_http_targets(check, delivered_apps)
        if not targets:
            reason = (
                REASON_NO_REGISTERED_APPS
                if check.target == REGISTERED_APPS_HTTP_TARGET
                else REASON_TARGET_NOT_REGISTERED
            )
            self.entries.append(
                _entry(
                    check.check_id,
                    CheckClass.HTTP,
                    CheckStatus.FAILED,
                    reason,
                    "nothing to probe for target {!r}".format(check.target),
                    "",
                )
            )
            return
        for index, target in enumerate(targets):
            await self._probe_one_url(check, index, target)

    async def _probe_one_url(self, check: HttpCheck, index: int, target: RegisteredApp) -> None:
        entry_id = "{}_{}".format(check.check_id, slugify(target.name))
        # The check id is part of the filename: two checks aimed at the same app would otherwise
        # both write probe 0 for it, and only the last write would survive.
        evidence_name = "{}/{}_{}_{}.json".format(HTTP_DIRNAME, check.check_id, index, slugify(target.name))
        if self._remaining_seconds <= 0:
            self.entries.append(_entry(entry_id, CheckClass.HTTP, CheckStatus.ERROR, REASON_TIMEOUT, target.url, ""))
            return
        is_success, output = await self._run_in_workspace(
            "http_probes", http_probe_command(target.url), _HTTP_TIMEOUT_SECONDS
        )
        sections = split_sections(output)
        probe_error = sections.get("probe_error", "")
        status_code, elapsed_seconds = parse_curl_status(sections.get("status", ""))
        body_head = sections.get("body", "")[:MAX_HTTP_BODY_BYTES]
        await self._write_evidence(
            evidence_name,
            json.dumps(
                {
                    "check_id": check.check_id,
                    "app": target.name,
                    "url": target.url,
                    "expect_status": check.expect_status,
                    "status_code": status_code,
                    "elapsed_seconds": elapsed_seconds,
                    "probe_error": probe_error.strip(),
                    "headers": ui_flows.bounded_tail(sections.get("headers", ""), MAX_COMMAND_OUTPUT_CHARS),
                    "body_head": body_head,
                },
                indent=2,
            ),
        )
        status, reason = http_entry_status(is_success, probe_error, status_code, body_head, check)
        if status is CheckStatus.PASSED:
            self.serving_app_names.add(target.name)
        self.entries.append(
            _entry(
                entry_id,
                CheckClass.HTTP,
                status,
                reason,
                "{} -> HTTP {} in {}s (expected {})".format(
                    target.url, status_code, elapsed_seconds, check.expect_status
                ),
                "{}/{}".format(VERIFICATION_DIRNAME, evidence_name),
            )
        )

    async def run_step_script(self, step_request: str) -> ui_flows.StepOutcome:
        """One flow step: one box exec of the step script, which acts, shoots and reads the page.

        Box-local, unlike everything else this collector runs -- the browser and the forward proxy
        both live here, and only the proxy's own tunnel touches the workspace. That is the whole
        latency argument for this executor.
        """
        wanted_seconds = flow_runner.STEP_TIMEOUT_SECONDS
        if self.flow_deadline:
            wanted_seconds = max(1, min(wanted_seconds, int(self.flow_deadline - time.monotonic())))
        result = await minds_bridge.run_in_box(
            self.environment, ui_flows.step_command(step_request), self.box_env, self._budget(wanted_seconds)
        )
        output = (result.stdout or "") + (result.stderr or "")
        self.trace.append(
            TraceRecord(
                timestamp=ui_flows.utc_now_iso(),
                phase="ui_flows",
                command=ui_flows.step_command(step_request)[:400],
                is_success=result.return_code == 0,
                output=ui_flows.bounded_tail(output, MAX_TRACE_OUTPUT_CHARS),
            )
        )
        return ui_flows.parse_step_result(result.stdout or "")

    async def _start_forward(self) -> str:
        """Start the trial's own `mngr forward` and wait until it actually serves.

        Readiness is a real request returning 200, not the proxy's `listening` event: that event
        fires from the server's lifespan hook before the socket accepts, and even once it accepts
        the proxy answers 503 until discovery has resolved the workspace. Returns the reason it
        could not be made ready, or empty on success.
        """
        argv = forward_instance.build_forward_command(
            self.preauth_cookie.get_secret_value(),
            self.browser_bridge_token.get_secret_value(),
            forward_instance.FORWARD_PORT,
        )
        start = forward_instance.forward_start_command(
            argv, minds_bridge.BOX_MNGR_DIR, forward_instance.BOX_FORWARD_LOG_PATH
        )
        # The cookie and the bridge token are arguments, so the command is never traced verbatim.
        await minds_bridge.run_in_box(self.environment, start, self.box_env, self._budget(PROBE_TIMEOUT_SECONDS))
        self.trace.append(
            TraceRecord(
                timestamp=ui_flows.utc_now_iso(),
                phase="ui_flows",
                command=forward_instance.redact_forward_command(argv),
                is_success=True,
                output="",
            )
        )
        probe = forward_instance.forward_probe_command(
            forward_instance.FORWARD_PORT, self.preauth_cookie.get_secret_value(), self.workspace_agent_id
        )
        for _attempt in range(_FORWARD_READY_ATTEMPT_COUNT):
            if self._remaining_seconds <= 0:
                return REASON_TIMEOUT
            result = await minds_bridge.run_in_box(
                self.environment, probe, self.box_env, self._budget(PROBE_TIMEOUT_SECONDS)
            )
            if (result.stdout or "").strip() == "200":
                return ""
            await asyncio.sleep(self.readiness_poll_seconds)
        await self._capture_forward_events()
        return ui_flows.REASON_FORWARD_UNREACHABLE

    async def _start_browser(self, flow_index: int) -> str:
        """Launch one flow's headless Chromium, and wait for its debug port to answer."""
        launch = ui_flows.browser_launch_command(flow_index)
        result = await minds_bridge.run_in_box(
            self.environment, launch, self.box_env, self._budget(PROBE_TIMEOUT_SECONDS)
        )
        self.trace.append(
            TraceRecord(
                timestamp=ui_flows.utc_now_iso(),
                phase="ui_flows",
                command=launch,
                is_success=result.return_code == 0,
                output=ui_flows.bounded_tail((result.stdout or "") + (result.stderr or ""), MAX_TRACE_OUTPUT_CHARS),
            )
        )
        if result.return_code != 0:
            return ui_flows.REASON_BROWSER_LAUNCH_FAILED
        for _attempt in range(ui_flows.BROWSER_READY_ATTEMPT_COUNT):
            if self._remaining_seconds <= 0:
                return REASON_TIMEOUT
            probe = await minds_bridge.run_in_box(
                self.environment,
                ui_flows.browser_probe_command(ui_flows.flow_browser_port(flow_index)),
                self.box_env,
                self._budget(PROBE_TIMEOUT_SECONDS),
            )
            if "webSocketDebuggerUrl" in (probe.stdout or ""):
                return ""
            await asyncio.sleep(self.readiness_poll_seconds)
        return ui_flows.REASON_CDP_CONNECT_FAILED

    async def _capture_forward_events(self) -> None:
        """Fold the proxy's own account of itself into the trace: when it started serving, and
        every backend failure it reported. That is what separates a dead proxy from a dead tunnel
        after the fact, without re-running anything."""
        log_text = await minds_bridge.read_box_file(
            self.environment, self.box_env, forward_instance.BOX_FORWARD_LOG_PATH
        )
        summary = forward_instance.summarize_forward_events(forward_instance.parse_forward_events(log_text))
        if summary:
            self.trace.append(
                TraceRecord(
                    timestamp=ui_flows.utc_now_iso(),
                    phase="ui_flows",
                    command="(mngr forward envelopes)",
                    is_success=True,
                    output=ui_flows.bounded_tail(summary, MAX_TRACE_OUTPUT_CHARS),
                )
            )

    async def _stop_forward(self) -> None:
        """Stop the trial's own instance, matched on the port it holds so a forward the minds
        backend spawned is never caught by this."""
        await self._capture_forward_events()
        await minds_bridge.run_in_box(
            self.environment,
            forward_instance.forward_stop_command(forward_instance.FORWARD_PORT),
            self.box_env,
            PROBE_TIMEOUT_SECONDS,
        )

    async def _run_ui_flows(self, expectations: ExpandedExpectations) -> None:
        """Drive each declared flow through the delivered app's forwarded origin.

        This runs LAST of the collection steps: it is the most expensive one and the one most
        likely to exhaust the budget, and everything before it is worth having even when it does.
        """
        if not expectations.ui_flow_checks:
            return
        started_at = time.monotonic()
        agent = self.verification_agent
        if agent is None:
            self._record_flow_error(
                expectations.ui_flow_checks,
                ui_flows.REASON_VERIFIER_AGENT_FAILED,
                "no verification agent was configured",
            )
            await self._finish_flow_phase(started_at)
            return
        delivered_apps = self._delivered_apps
        if delivered_apps is None:
            # A delivered set we could not resolve tells us nothing about what was served, which is
            # the harness failing to look -- quite unlike a registry that lists nothing.
            self._record_flow_error(
                expectations.ui_flow_checks,
                self._unresolved_reason,
                "the delivered apps could not be resolved, so there is no origin to drive a flow against",
            )
            await self._finish_flow_phase(started_at)
            return
        if not forward_instance.is_agent_id(self.workspace_agent_id):
            # The agent id is the origin coordinate, so one the proxy does not route on leaves no
            # origin to build, whatever the workspace is serving.
            self._record_flow_error(
                expectations.ui_flow_checks,
                ui_flows.REASON_WORKSPACE_UNADDRESSABLE,
                "workspace agent id {!r} is not an origin coordinate, so no forwarded origin can be addressed".format(
                    self.workspace_agent_id
                ),
            )
            await self._finish_flow_phase(started_at)
            return
        target_url = self._flow_target_url(delivered_apps)
        if not target_url:
            # A readable registry listing no delivered app is the agent shipping nothing.
            for check in expectations.ui_flow_checks:
                self.entries.append(
                    _flow_entry(
                        check,
                        CheckStatus.FAILED,
                        ui_flows.REASON_NO_APP_TO_OPEN,
                        "no delivered app is registered, so there is no UI to exercise",
                    )
                )
            await self._finish_flow_phase(started_at)
            return
        await minds_bridge.upload_flow_step_script(self.environment, ui_flows.BOX_FLOW_STEP_PATH)
        forward_reason = await self._start_forward()
        if forward_reason:
            self._record_flow_error(expectations.ui_flow_checks, forward_reason, "the forward proxy never served")
            await self._stop_forward()
            await self._finish_flow_phase(started_at)
            return
        for flow_index, check in enumerate(expectations.ui_flow_checks):
            await self._run_one_flow(check, flow_index, target_url, agent)
        await self._stop_forward()
        await self._finish_flow_phase(started_at)

    async def _finish_flow_phase(self, started_at: float) -> None:
        self.flow_deadline = 0.0
        self._record_phase("ui_flows", started_at)
        await self._flush_record()

    def _flow_target_url(self, delivered_apps: Sequence[RegisteredApp]) -> str:
        """The forwarded origin of the app a flow drives: its label on the workspace's agent-keyed origin.

        Empty means the workspace registered nothing to drive, which is the agent's shortfall; the
        caller has already established that the agent id is addressable.

        An app that ANSWERED its root-path probe wins over one that merely holds a registry row.
        With more than one delivered row, taking the first would point the flow at whichever
        registered first, and a row whose port is dead serves the proxy's own error page -- so the
        flow would record the deliverable as broken having never once reached it. Registry order
        decides only among apps that are equally reachable, and remains the fallback when nothing
        was probed at all.

        The origin is built from the row's LABEL, not its service name: the label is the
        unguessable `<name>-<rand>` component forward_port.py mints and the proxy routes on,
        mapping it back to the service itself. A row with no label predates labels and routes under
        its name.
        """
        addressable = [app for app in delivered_apps if app.url]
        serving = [app for app in addressable if app.name in self.serving_app_names]
        for app in serving or addressable:
            return forward_instance.forwarded_origin(
                app.label or app.name, self.workspace_agent_id, forward_instance.FORWARD_PORT
            )
        return ""

    def _record_flow_error(self, checks: Sequence[UiFlowCheck], reason: str, detail: str) -> None:
        for check in checks:
            self.entries.append(
                _flow_entry(check, CheckStatus.ERROR, reason, ui_flows.bounded_tail(detail, MAX_COMMAND_OUTPUT_CHARS))
            )

    async def _run_one_flow(
        self, check: UiFlowCheck, flow_index: int, target_url: str, agent: ui_flows.VerificationAgent
    ) -> None:
        """Execute one flow in a browser of its own, and record what it produced."""
        slug = slugify(check.name)
        self.flow_deadline = min(time.monotonic() + flow_runner.FLOW_DEADLINE_SECONDS, self.deadline)

        # A browser of its own per flow, so one flow's cookies and storage never leak into the next.
        browser_reason = await self._start_browser(flow_index)
        if browser_reason:
            await self._finish_flow(
                check, slug, (), CheckStatus.ERROR, browser_reason, "the box browser never came up"
            )
            return
        executor = _BoxFlowStepExecutor(
            collector=self,
            slug=slug,
            cdp_endpoint_url=ui_flows.cdp_endpoint(ui_flows.flow_browser_port(flow_index)),
        )
        run = await flow_runner.run_flow(
            check, target_url, agent, executor, phase_deadline=self.deadline, flow_deadline=self.flow_deadline
        )
        await self._finish_flow(check, slug, run.records, run.status, run.reason, run.detail)

    def flow_screenshot_path(self, slug: str, step_index: int) -> str:
        """Where the step script writes a frame: straight into the box's evidence directory.

        The browser runs in the box, so the screenshot is already where the declared artifact
        collector will find it -- there is no workspace staging leg and no rsync at all, which is
        the transport the fleet executor needed and this one does not.
        """
        return "{}/{}/{}/{}".format(self._box_dir, FLOWS_DIRNAME, slug, ui_flows.flow_screenshot_name(step_index))

    async def _finish_flow(
        self, check: UiFlowCheck, slug: str, records: Sequence[str], status: CheckStatus, reason: str, detail: str
    ) -> None:
        """Write one flow's step log and record its entry. Screenshots are already in place."""
        await self._write_evidence(
            "{}/{}/{}".format(FLOWS_DIRNAME, slug, FLOW_LOG_FILENAME), "".join(line + "\n" for line in records)
        )
        self.entries.append(
            _flow_entry(check, status, reason, ui_flows.bounded_tail(detail, MAX_COMMAND_OUTPUT_CHARS))
        )
        await self._flush_record()

    def _evaluate_process_checks(self, expectations: ExpandedExpectations) -> None:
        """How the agent worked, rather than what it delivered: which skills it invoked and how many
        background workers it launched, read off the captured transcript stream.

        The stream is the same input the worker capture scans, and it is already host-side, so this
        costs no round trip. Without it nothing about the agent's own work can be read, and every
        check is an ERROR -- the instrument failed, and an agent must not be charged for that.

        A stream that came out but holds no agent record at all is the same failure wearing the
        shape of a success. These checks run only on a finished conversation, where every configured
        entry was answered, so the chat agent's own stream cannot honestly be free of its steps --
        and reading an empty one as evidence would charge the agent for every required skill while
        waving off however many workers it launched.
        """
        if not expectations.process_checks:
            return
        started_at = time.monotonic()
        stream = self.transcript_capture.stream
        records = parse_transcript_jsonl(stream.host_path.read_text()) if stream.host_path is not None else []
        if any(record.get("source") == "agent" for record in records):
            invocations = scan_skill_invocations(records)
            launches = scan_worker_launches(records, depth=0, lead_name="")
            self.entries.extend(_process_entry(check, invocations, launches) for check in expectations.process_checks)
        else:
            is_uncaptured = stream.host_path is None
            detail = (
                "the transcript stream was not captured: {} {}".format(
                    stream.failure_reason, stream.failure_detail
                ).strip()
                if is_uncaptured
                else "the captured transcript stream holds no record of the agent"
            )
            self.entries.extend(
                _entry(
                    check.check_id,
                    CheckClass.PROCESS,
                    CheckStatus.ERROR,
                    REASON_TRANSCRIPT_UNCAPTURED if is_uncaptured else REASON_TRANSCRIPT_EMPTY,
                    detail,
                    "",
                )
                for check in expectations.process_checks
            )
        self._record_phase("process_checks", started_at)

    def _evaluate_timing_checks(self, expectations: ExpandedExpectations) -> None:
        """How long the agent took to satisfy the goal-holding client, read off the conversation
        records the driver holds. Costs no round trip: nothing about it is in the workspace.

        Runs after every other class, so the prerequisite is answered from the failures this bundle
        actually recorded rather than from a second pass over the workspace.
        """
        if not expectations.timing_checks:
            return
        started_at = time.monotonic()
        timing = measure_goal_satisfaction_timing(self.entry_records, self.turn_records)
        failed_classes = frozenset(entry.check_class for entry in self.entries if entry.status == CheckStatus.FAILED)
        for check in expectations.timing_checks:
            self.entries.append(
                _timing_entry(
                    check,
                    timing,
                    tuple(
                        check_class
                        for check_class in sorted(set(check.requires_no_failures))
                        if check_class in failed_classes
                    ),
                )
            )
        self._record_phase("timing_checks", started_at)

    def _evaluate_app_checks(self, expectations: ExpandedExpectations) -> None:
        """The registry/service half of the deliverable: enough delivered apps registered, and each
        one's supervisord program actually running. Derived from the always-on capture, so it costs
        no extra round trip."""
        if not expectations.app_checks:
            return
        started_at = time.monotonic()
        service_states = parse_service_states(self.services_text)
        delivered_apps = self._delivered_apps
        program_by_registration = parse_supervised_registrations(self.supervisord_conf)
        is_supervisord_conf_readable = not is_supervisord_capture_broken(self.supervisord_conf, service_states)
        for check in expectations.app_checks:
            self.entries.append(
                registration_entry(check.check_id, check.min_registered_apps, delivered_apps, self._unresolved_reason)
            )
            # An unresolved delivered set tells us nothing about the services behind it either, so
            # there is no per-app entry to record; the registration entry already carries the error.
            if check.is_supervisord_service_required and delivered_apps is not None:
                self.entries.extend(
                    service_entries(
                        check.check_id,
                        delivered_apps,
                        service_states,
                        program_by_registration,
                        is_services_readable=bool(service_states),
                        is_supervisord_conf_readable=is_supervisord_conf_readable,
                    )
                )
        self._record_phase("app_checks", started_at)


class _BoxFlowStepExecutor(flow_runner.FlowStepExecutor):
    """The trial-time executor: each step is one exec of the step script in the box, against the
    browser the collector launched for this flow, writing its frame into the box's evidence dir.

    The session cookie rides the OPENING request only, so the flow's first navigation is already
    authenticated; later steps land in the same browser, which is still holding the session. The
    scope it is installed at is `forward_instance.session_cookie_domain`.
    """

    collector: EvidenceCollector = Field(frozen=True, description="Owns the bridge, the budget and the trace")
    slug: str = Field(frozen=True, description="The flow's evidence directory name under flows/")
    cdp_endpoint_url: str = Field(frozen=True, description="Where this flow's box browser listens for CDP")

    async def run_step(self, action: ui_flows.FlowAction, step_index: int) -> ui_flows.StepOutcome:
        is_opening = step_index == 0
        return await self.collector.run_step_script(
            ui_flows.build_step_request(
                action,
                self.collector.flow_screenshot_path(self.slug, step_index),
                cdp_endpoint_url=self.cdp_endpoint_url,
                preauth_cookie=self.collector.preauth_cookie.get_secret_value() if is_opening else "",
                cookie_domain=(
                    forward_instance.session_cookie_domain(self.collector.workspace_agent_id) if is_opening else ""
                ),
            )
        )


@pure
def oracle_evidence_files(case: CaseConfig) -> dict[str, str]:
    """The green evidence bundle the oracle fabricates, so `-a oracle` exercises the whole new path
    -- artifact transfer, the outcome criteria, the judge, and the reward composition -- without
    booting a workspace. Every declared check is recorded as passed against a plausible registry."""
    expectations = case.expectations
    assert expectations is not None, "oracle evidence is only fabricated for cases that declare expectations"
    oracle_apps = (*_ORACLE_PREEXISTING_APPS, (_ORACLE_APP_NAME, _ORACLE_APP_URL))
    apps_toml = "".join(
        '[[apps]]\nname = "{name}"\nurl = "{url}"\nlabel = "{name}-o1r2a3c4"\n\n'.format(name=name, url=url)
        for name, url in oracle_apps
    )
    services = "".join(
        "{:<32} RUNNING   pid {}, uptime 0:05:00\n".format(name, 101 + index)
        for index, (name, _url) in enumerate(oracle_apps)
    )
    inventory = "".join(
        json.dumps({"path": path, "size_bytes": 1024, "mtime": 0.0}) + "\n"
        for path in _oracle_inventory_paths(expectations)
    )
    manifest = EvidenceManifest(
        schema_version=MANIFEST_SCHEMA_VERSION,
        case_id=case.case_id,
        base_sha="0" * 40,
        dwt_tip_sha="d" * 40,
        preexisting_registrations=tuple(sorted(name for name, _url in _ORACLE_PREEXISTING_APPS)),
        is_expectations_declared=True,
        is_evidence_complete=True,
        started_at="1970-01-01T00:00:00+00:00",
        phases=(PhaseTiming(name="oracle", seconds=0.0),),
        entries=_oracle_entries(expectations, _oracle_satisfied_turn_index(case)),
    )
    trace = TraceRecord(
        timestamp="1970-01-01T00:00:00+00:00",
        phase="oracle",
        command="(oracle run: no workspace was booted, so the evidence is fabricated)",
        is_success=True,
        output="",
    )
    return {
        APPS_REGISTRY_FILENAME: apps_toml,
        SERVICES_FILENAME: services,
        FILE_INVENTORY_FILENAME: inventory,
        REPO_STATE_FILENAME: json.dumps(
            {
                "repo_root": DEFAULT_WORKSPACE_REPO_ROOT,
                "base_sha": "0" * 40,
                "dwt_tip_sha": "d" * 40,
                "head_sha": "1" * 40,
                "commit_count_beyond_base": "2",
                "is_clean": True,
                "status_porcelain": "",
            },
            indent=2,
        ),
        TRACE_FILENAME: json.dumps(trace.model_dump(mode="json")) + "\n",
        MANIFEST_FILENAME: json.dumps(manifest.model_dump(mode="json"), indent=2),
        **{
            _oracle_http_evidence_name(check): json.dumps(
                {
                    "check_id": check.check_id,
                    "app": _ORACLE_APP_NAME,
                    "url": _ORACLE_APP_URL,
                    "expect_status": check.expect_status,
                    "status_code": check.expect_status,
                    "elapsed_seconds": 0.01,
                    "probe_error": "",
                    "headers": "HTTP/1.1 {} OK\r\ncontent-type: text/html\r\n".format(check.expect_status),
                    "body_head": "<!doctype html><title>{}</title>".format(case.case_id),
                },
                indent=2,
            )
            for check in expectations.http_checks
        },
        # Flow logs but no screenshots: the oracle boots no browser, and the judge prompt states
        # that screenshots may be absent. The grade-time pre-step still runs, which is what makes
        # an oracle run prove the empty-screenshot-directory path leaves no "[not found]" noise.
        **{
            "{}/{}/{}".format(FLOWS_DIRNAME, slugify(check.name), FLOW_LOG_FILENAME): _oracle_flow_log(check)
            for check in expectations.ui_flow_checks
        },
    }


@pure
def _oracle_flow_log(check: UiFlowCheck) -> str:
    """The log a flow would have written had the delivered app been perfect: the same record kinds a
    real flow emits, so the oracle exercises every path a reader takes."""
    opening_state = "browser minds-eval-verify @ {}  ({})".format(_ORACLE_APP_URL, check.name)
    return "".join(
        line + "\n"
        for line in (
            ui_flows.flow_init_record(
                check.actions,
                check.expect,
                _ORACLE_APP_URL,
                opening_state,
                "",
                "1970-01-01T00:00:00+00:00",
            ),
            ui_flows.flow_step_record(
                1,
                "finish the flow",
                "",
                "the delivered app, open and showing what the declared actions describe",
                "nothing further -- every declared action has been carried out",
                "",
                StepReaction.UNOBSERVED,
                "{}\n{}".format(opening_state, check.expect),
                "",
                "",
                "1970-01-01T00:00:00+00:00",
            ),
            # A reading, not a verdict, exactly as a real flow's closing record is: the digest prints
            # it as one more piece of evidence and the judge still rules on the `expect` itself.
            ui_flows.flow_final_record(
                2,
                "the page shows: {}".format(check.expect),
                "{}\n{}".format(opening_state, check.expect),
                "1970-01-01T00:00:00+00:00",
            ),
        )
    )


@pure
def _oracle_http_evidence_name(check: HttpCheck) -> str:
    return "{}/{}_0_{}.json".format(HTTP_DIRNAME, check.check_id, slugify(_ORACLE_APP_NAME))


@pure
def _oracle_inventory_paths(expectations: ExpandedExpectations) -> tuple[str, ...]:
    """Inventory paths that satisfy every declared glob, plus a plausible app source tree.

    A glob's wildcards are filled in literally, which covers `*` but not `?` or a character class:
    a case using those would see its oracle run score below the usual floor rather than fail loudly.
    """
    return (
        "workspace/apps/{}/main.py".format(_ORACLE_APP_NAME),
        "workspace/apps/{}/templates/index.html".format(_ORACLE_APP_NAME),
        *tuple(check.glob.replace("*", "oracle") for check in expectations.files_checks),
    )


@pure
def _oracle_satisfied_turn_index(case: CaseConfig) -> int:
    """Which client message the oracle's goal-holding client was satisfied by.

    The oracle's canned conversation is one message per prompts entry, and its state.json records a
    goal entry as satisfied by its own message, so that is the entry's 1-based position. A case with
    no goal entry has no such message; the last one stands in, which keeps the fabricated timing
    entry self-consistent with the fabricated conversation.
    """
    return max(
        (index + 1 for index, entry in enumerate(case.prompts) if isinstance(entry, GoalEntry)),
        default=len(case.prompts),
    )


@pure
def _oracle_entries(expectations: ExpandedExpectations, satisfied_turn_index: int) -> tuple[ManifestEntry, ...]:
    entries: list[ManifestEntry] = [
        _entry(
            "file_inventory",
            CheckClass.FILES,
            CheckStatus.PASSED,
            "",
            "2 file(s) inventoried",
            "{}/{}".format(VERIFICATION_DIRNAME, FILE_INVENTORY_FILENAME),
        )
    ]
    if expectations.is_deliverable_bundle_required:
        entries.append(
            _entry(
                "deliverable_bundle",
                CheckClass.BUNDLE,
                CheckStatus.PASSED,
                "",
                "HEAD 111111111111 (2 commit(s) beyond the prepared clone); clean",
                "",
            )
        )
    for check in expectations.app_checks:
        entries.append(
            registration_entry(
                check.check_id,
                check.min_registered_apps,
                (
                    RegisteredApp(
                        name=_ORACLE_APP_NAME,
                        url=_ORACLE_APP_URL,
                        label=_ORACLE_APP_LABEL,
                        is_preexisting=False,
                        is_internal=False,
                    ),
                ),
                unresolved_reason="",
            )
        )
        if check.is_supervisord_service_required:
            entries.extend(
                service_entries(
                    check.check_id,
                    (
                        RegisteredApp(
                            name=_ORACLE_APP_NAME,
                            url=_ORACLE_APP_URL,
                            label=_ORACLE_APP_LABEL,
                            is_preexisting=False,
                            is_internal=False,
                        ),
                    ),
                    {_ORACLE_APP_NAME: "RUNNING"},
                    {_ORACLE_APP_NAME: _ORACLE_APP_NAME},
                    is_services_readable=True,
                    is_supervisord_conf_readable=True,
                )
            )
    for check in expectations.http_checks:
        entries.append(
            _entry(
                "{}_{}".format(check.check_id, slugify(_ORACLE_APP_NAME)),
                CheckClass.HTTP,
                CheckStatus.PASSED,
                "",
                "{} -> HTTP {} in 0.01s (expected {})".format(
                    _ORACLE_APP_URL, check.expect_status, check.expect_status
                ),
                "{}/{}".format(VERIFICATION_DIRNAME, _oracle_http_evidence_name(check)),
            )
        )
    for index, command in enumerate(expectations.test_commands):
        entries.append(
            _entry(
                "test_command_{}".format(index),
                CheckClass.TEST_COMMAND,
                CheckStatus.PASSED,
                "",
                "$ {}\nexit 0\n".format(command),
                "",
            )
        )
    for check in expectations.ui_flow_checks:
        entries.append(
            _flow_entry(
                check,
                CheckStatus.PASSED,
                "",
                "expected: {}\nagent's reading of the final state: as described".format(check.expect),
            )
        )
    # Graded by the same function a live trial uses, against a transcript in which every required
    # skill was invoked and no worker was ever launched, so the fabricated entries keep the shape a
    # real run records.
    oracle_invocations = tuple(
        SkillInvocation(name=check.skill, tool_call_id=_ORACLE_SKILL_CALL_ID, step_id="2")
        for check in expectations.process_checks
        if check.kind is ProcessCheckKind.REQUIRED_SKILL
    )
    entries.extend(_process_entry(check, oracle_invocations, ()) for check in expectations.process_checks)
    # Built by the same function a live trial uses, against a conversation that reached the client's
    # goal exactly at the case's fast anchor and left no prerequisite class failing -- so an oracle
    # run scores the class's full mark instead of erroring for want of a measurement.
    entries.extend(
        _timing_entry(
            check,
            GoalSatisfactionTiming(seconds=check.fast_seconds, turn_index=satisfied_turn_index),
            (),
        )
        for check in expectations.timing_checks
    )
    return tuple(entries)

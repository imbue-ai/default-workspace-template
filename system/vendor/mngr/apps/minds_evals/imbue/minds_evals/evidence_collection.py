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
import re
import shlex
import time
import tomllib
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final

from harbor.environments.base import BaseEnvironment
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.minds_evals import forward_instance
from imbue.minds_evals import minds_bridge
from imbue.minds_evals import ui_flows
from imbue.minds_evals.data_types import CaseConfig
from imbue.minds_evals.data_types import CheckClass
from imbue.minds_evals.data_types import CheckStatus
from imbue.minds_evals.data_types import EvidenceEnv
from imbue.minds_evals.data_types import EvidenceManifest
from imbue.minds_evals.data_types import ExpandedExpectations
from imbue.minds_evals.data_types import HttpCheck
from imbue.minds_evals.data_types import ManifestEntry
from imbue.minds_evals.data_types import PhaseTiming
from imbue.minds_evals.data_types import REGISTERED_APPS_HTTP_TARGET
from imbue.minds_evals.data_types import RegisteredApp
from imbue.minds_evals.data_types import TraceRecord
from imbue.minds_evals.data_types import UiFlowCheck
from imbue.minds_evals.expectations import slugify

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
APPS_REGISTRY_RELATIVE_PATH: Final[str] = "data/.state/apps.toml"
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
_BUNDLE_TIMEOUT_SECONDS: Final[int] = 300
_TEST_COMMAND_TIMEOUT_SECONDS: Final[int] = 300
_HTTP_TIMEOUT_SECONDS: Final[int] = 60
_RSYNC_TIMEOUT_SECONDS: Final[int] = 600
# One flow step: a box-local exec that drives the browser and reads the page back. Generous
# because a navigation waits for the network to settle and a heavy page's ARIA tree is large.
_STEP_TIMEOUT_SECONDS: Final[int] = 120
# How long the forward proxy gets to start serving. It has to bind, then discover the workspace,
# then bring up its SSH tunnel, and it answers 503 throughout.
_FORWARD_READY_ATTEMPT_COUNT: Final[int] = 40
_FORWARD_READY_POLL_SECONDS: Final[float] = 3.0
# One flow's own wall-clock. Separate from the phase budget on purpose: exceeding this is the app
# failing to respond, whereas exhausting the phase budget is the harness running out of time.
# Re-measured against the box-side executor on its first live run rather than carried over from the
# fleet's ~30s/step, which was dominated by a workspace hop this executor does not make.
_FLOW_DEADLINE_SECONDS: Final[float] = 600.0

# What a probe prints in a `*_status` section when the file that section reports on was there to
# read. Anything else -- including the empty section a probe that died mid-command leaves behind --
# means the file could not be read, which is a different claim from a file that was read and lists
# nothing.
STATUS_PRESENT: Final[str] = "present"

_SECTION_MARKER: Final[str] = "<<<MINDS_EVALS_SECTION:{}>>>"
_SECTION_PATTERN: Final[re.Pattern[str]] = re.compile(r"<<<MINDS_EVALS_SECTION:([a-z_]+)>>>\n?")

# Reasons recorded on non-passing entries, so a manifest reader never has to parse prose.
REASON_TIMEOUT: Final[str] = "timeout"
REASON_BRIDGE_FAILED: Final[str] = "bridge_failed"
REASON_REPO_NOT_FOUND: Final[str] = "repo_not_found"
REASON_REGISTRY_ABSENT: Final[str] = "registry_absent"
REASON_REGISTRY_UNREADABLE: Final[str] = "registry_unreadable"
# The pre-turn-1 registry snapshot could not be taken, so nothing in the registry can be told apart
# from what the workspace was already serving before the agent ran.
REASON_PREEXISTING_UNKNOWN: Final[str] = "preexisting_unknown"
REASON_SERVICES_UNREADABLE: Final[str] = "services_unreadable"
REASON_PROBE_UNAVAILABLE: Final[str] = "probe_unavailable"
REASON_NO_REGISTERED_APPS: Final[str] = "no_registered_apps"
REASON_TARGET_NOT_REGISTERED: Final[str] = "target_not_registered"
REASON_WRONG_STATUS: Final[str] = "wrong_status"
REASON_BODY_MISMATCH: Final[str] = "body_mismatch"
REASON_SERVICE_NOT_RUNNING: Final[str] = "service_not_running"
REASON_NO_SUPERVISED_PROGRAM: Final[str] = "no_supervised_program"
REASON_TOO_FEW_APPS: Final[str] = "too_few_apps"
REASON_NONZERO_EXIT: Final[str] = "nonzero_exit"

# The name the oracle's fabricated evidence gives the app it pretends was delivered, and the
# template rows it pretends were already there, so the fabricated bundle exercises the same
# delivered-versus-pre-existing resolution a live trial does.
_ORACLE_PREEXISTING_APPS: Final[tuple[tuple[str, str], ...]] = (
    ("system_interface", "http://localhost:8000"),
    ("terminal", "http://localhost:7681"),
)
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
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@pure
def _bounded(text: str, max_chars: int) -> str:
    """The tail of a long output, marked so a reader knows it was cut rather than empty."""
    if len(text) <= max_chars:
        return text
    return "[...truncated...]\n" + text[-max_chars:]


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
    this single probe -- the registry it captured and the `system/supervisord.conf` it catted, which
    before the first turn is still the pinned template's file verbatim -- so they are decoded here,
    in the module that prints the probe's sections. See `resolve_preexisting_registrations` for why
    one source is not enough.

    None covers a registry that is not there yet as well as one that could not be parsed: either way
    nothing in it can be called pre-existing.
    """
    sections = split_sections(output)
    if not is_registry_status_present(sections):
        return None
    return resolve_preexisting_registrations(
        parse_registry_names(sections.get("registry", "")),
        frozenset(parse_supervised_registrations(sections.get("supervisord", ""))),
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
# writes --name first, and a hand-written block may do either.
_FORWARD_PORT_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"forward_port\.py[^\n]*?--name\s+([\w-]+)")
_PROGRAM_SECTION_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\[program:([^\]]+)\]", re.MULTILINE)


@pure
def parse_supervised_registrations(supervisord_conf: str) -> dict[str, str]:
    """Registry name -> the supervisord program whose block registers it.

    The join goes through the `forward_port.py` invocations inside each `[program:*]` block rather
    than through name equality, because the two are not the same thing: a multi-port app registers
    extra origin-label rows (`<name>-admin`) that have no program of their own, and a program is
    free to register a row under any name. This is the same join the workspace template's own
    migration tooling uses.
    """
    program_by_registration: dict[str, str] = {}
    matches = list(_PROGRAM_SECTION_PATTERN.finditer(supervisord_conf))
    for index, match in enumerate(matches):
        block_end = matches[index + 1].start() if index + 1 < len(matches) else len(supervisord_conf)
        block = supervisord_conf[match.end() : block_end]
        for registration in _FORWARD_PORT_NAME_PATTERN.findall(block):
            program_by_registration.setdefault(registration, match.group(1).strip())
    return program_by_registration


@pure
def resolve_preexisting_registrations(
    registry_names: frozenset[str] | None, config_registrations: frozenset[str]
) -> frozenset[str] | None:
    """What the workspace already served before the agent ran, or None if that cannot be told.

    Both arguments are read from the same pre-turn-1 snapshot, because neither alone is complete:

    - ``registry_names`` is the app registry as it actually stood. A measurement rather than an
      inference, and the only source that sees a template app which registers from inside the script
      its supervisord program runs rather than from a ``forward_port.py`` call in the config itself.
      The terminal does exactly that, as do the owner-exec and vm-exec daemons, and counting one as
      a deliverable is the failure this resolution exists to prevent.
    - ``config_registrations`` is what the workspace's own ``system/supervisord.conf`` registers,
      joined through its ``forward_port.py --name`` invocations. It covers a template app whose
      service is slow enough that it had not registered its port yet when the snapshot was taken:
      the file is on disk from the moment the workspace is cloned, whatever its services are doing.
      Directory names under ``system/apps/`` would not do -- a registry name is a caller-supplied
      ``--name`` flag, and a multi-port app registers extra origin-label rows that correspond to no
      directory at all.

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
        'if [ -n "$root" ]; then cat "$root/{supervisord_path}" 2>/dev/null; fi; '
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
        registry_path=APPS_REGISTRY_RELATIVE_PATH,
        supervisord_path=SUPERVISORD_CONF_RELATIVE_PATH,
        instances_path=ISOLATED_INSTANCES_RELATIVE_PATH,
        instance_file=shlex.quote(ISOLATED_INSTANCE_FILENAME),
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
) -> tuple[ManifestEntry, ...]:
    """One entry per delivered app: an app whose supervising program is not running is exactly the
    "started it, then it crashed" failure the liveness checks exist to catch.

    The registry row is mapped to its program through the config's `forward_port.py` invocations,
    not by assuming the two share a name -- a multi-port app registers extra origin-label rows that
    no program owns. A row no program registers at all is a real shortfall of the minds-app contract
    (the app was started by hand and would not survive a restart), recorded under its own reason so
    it stays distinguishable from a program that exists and crashed.
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
            entries.append(
                _entry(
                    entry_id,
                    CheckClass.APP,
                    CheckStatus.FAILED,
                    REASON_NO_SUPERVISED_PROGRAM,
                    "no supervisord program registers {}, so nothing supervises it".format(app.name),
                    "",
                )
            )
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
    case: CaseConfig = Field(frozen=True, description="The case whose expanded expectations drive the probes")
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
    workspace_host_id: str = Field(
        frozen=True, default="", description="The workspace's mngr host id; the forwarded origin's host component"
    )
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
    supervisord_conf: str = Field(default="", description="The workspace's supervisord config as captured")
    isolated_instance_services: frozenset[str] = Field(
        default=frozenset(), description="Registry rows owned by throwaway preview servers"
    )
    # None until the registry has been read, and again if it turned out to be unreadable.
    registered_apps: tuple[RegisteredApp, ...] | None = Field(default=None, description="The parsed registry")
    serving_app_names: set[str] = Field(
        default_factory=set, description="Delivered apps that answered their root-path probe as expected"
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
                timestamp=utc_now_iso(),
                phase=phase,
                command=command,
                is_success=is_success,
                output=_bounded(output, MAX_TRACE_OUTPUT_CHARS),
            )
        )
        return is_success, output

    async def _pull_staged_file(self, phase: str, filename: str) -> bool:
        """Pull one workspace-staged file into the box's evidence directory over the snapshot
        transport. Rsyncing a named file INTO a directory keeps its basename, which is what makes
        the staged name the bundle name."""
        command = "cd {mngr} && uv run mngr rsync {agent}:{src} {box_dir}/".format(
            mngr=minds_bridge.BOX_MNGR_DIR,
            agent=shlex.quote(self.workspace_agent_id),
            src="{}/{}".format(WORKSPACE_STAGING_DIR, filename),
            box_dir=self._box_dir,
        )
        result = await minds_bridge.run_in_box(
            self.environment, command, self.box_env, self._budget(_RSYNC_TIMEOUT_SECONDS)
        )
        is_success = result.return_code == 0
        self.trace.append(
            TraceRecord(
                timestamp=utc_now_iso(),
                phase=phase,
                command=command,
                is_success=is_success,
                output=_bounded((result.stdout or "") + (result.stderr or ""), MAX_TRACE_OUTPUT_CHARS),
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
            started_at=self.started_at or utc_now_iso(),
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
        self.started_at = utc_now_iso()
        # Idempotent: setup already created this so the declared artifact always exists, but a
        # collector run against a box that skipped setup must not write into a missing directory.
        await ensure_evidence_dir(self.environment)
        await self._capture_workspace_state()
        await self._capture_file_inventory()
        expectations = self.case.expectations
        if is_expectations_collection_wanted and expectations is not None:
            if expectations.is_deliverable_bundle_required:
                await self._capture_repo_state()
            await self._run_test_commands(expectations)
            await self._run_http_probes(expectations)
            self._evaluate_app_checks(expectations)
            # Last, and after the HTTP probes: driving the UI is the most expensive step and the
            # one most likely to exhaust the budget, and everything above is worth having anyway.
            await self._run_ui_flows(expectations)
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
                    _bounded(output, MAX_COMMAND_OUTPUT_CHARS),
                    "",
                )
            )
        self._record_phase("workspace_state", started_at)
        await self._flush_record()

    async def _capture_file_inventory(self) -> None:
        started_at = time.monotonic()
        is_success, output = await self._run_in_workspace(
            "file_inventory", file_inventory_command(), _INVENTORY_TIMEOUT_SECONDS
        )
        is_pulled = await self._pull_staged_file("file_inventory", FILE_INVENTORY_FILENAME) if is_success else False
        self.entries.append(
            _entry(
                "file_inventory",
                CheckClass.FILES,
                CheckStatus.PASSED if is_pulled else CheckStatus.ERROR,
                "" if is_pulled else _timeout_or(REASON_BRIDGE_FAILED, self._remaining_seconds),
                "{} file(s) inventoried".format(output.strip())
                if is_pulled
                else _bounded(output, MAX_COMMAND_OUTPUT_CHARS),
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
            await self._pull_staged_file("repo_state", DELIVERABLE_BUNDLE_FILENAME)
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
                    "status_porcelain": _bounded(porcelain, MAX_COMMAND_OUTPUT_CHARS),
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
                        command, exit_code or "unknown", _bounded(sections.get("output", ""), MAX_COMMAND_OUTPUT_CHARS)
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
                    "headers": _bounded(sections.get("headers", ""), MAX_COMMAND_OUTPUT_CHARS),
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

    async def _run_step_script(self, step_request: str) -> ui_flows.StepOutcome:
        """One flow step: one box exec of the step script, which acts, shoots and reads the page.

        Box-local, unlike everything else this collector runs -- the browser and the forward proxy
        both live here, and only the proxy's own tunnel touches the workspace. That is the whole
        latency argument for this executor.
        """
        wanted_seconds = _STEP_TIMEOUT_SECONDS
        if self.flow_deadline:
            wanted_seconds = max(1, min(wanted_seconds, int(self.flow_deadline - time.monotonic())))
        result = await minds_bridge.run_in_box(
            self.environment, ui_flows.step_command(step_request), self.box_env, self._budget(wanted_seconds)
        )
        output = (result.stdout or "") + (result.stderr or "")
        self.trace.append(
            TraceRecord(
                timestamp=utc_now_iso(),
                phase="ui_flows",
                command=ui_flows.step_command(step_request)[:400],
                is_success=result.return_code == 0,
                output=_bounded(output, MAX_TRACE_OUTPUT_CHARS),
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
                timestamp=utc_now_iso(),
                phase="ui_flows",
                command=forward_instance.redact_forward_command(argv),
                is_success=True,
                output="",
            )
        )
        probe = forward_instance.forward_probe_command(
            forward_instance.FORWARD_PORT, self.preauth_cookie.get_secret_value(), self.workspace_host_id
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
                timestamp=utc_now_iso(),
                phase="ui_flows",
                command=launch,
                is_success=result.return_code == 0,
                output=_bounded((result.stdout or "") + (result.stderr or ""), MAX_TRACE_OUTPUT_CHARS),
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
                    timestamp=utc_now_iso(),
                    phase="ui_flows",
                    command="(mngr forward envelopes)",
                    is_success=True,
                    output=_bounded(summary, MAX_TRACE_OUTPUT_CHARS),
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
        if not forward_instance.is_host_id(self.workspace_host_id):
            # Without the host id there is no origin to build, whatever the workspace is serving. That
            # is the harness failing to look it up, so it must not be charged to the agent the way an
            # empty registry is.
            self._record_flow_error(
                expectations.ui_flow_checks,
                ui_flows.REASON_HOST_ID_UNKNOWN,
                "no usable workspace host id ({!r}), so no forwarded origin can be addressed".format(
                    self.workspace_host_id
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
        """The forwarded origin of the app a flow drives -- the URL the client's app tab iframes.

        Empty means the workspace registered nothing to drive, which is the agent's shortfall; the
        caller has already established that the host id is usable, so that harness-side failure can
        never reach this answer.

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
                app.label or app.name, self.workspace_host_id, forward_instance.FORWARD_PORT
            )
        return ""

    def _record_flow_error(self, checks: Sequence[UiFlowCheck], reason: str, detail: str) -> None:
        for check in checks:
            self.entries.append(
                _flow_entry(check, CheckStatus.ERROR, reason, _bounded(detail, MAX_COMMAND_OUTPUT_CHARS))
            )

    async def _run_one_flow(
        self, check: UiFlowCheck, flow_index: int, target_url: str, agent: ui_flows.VerificationAgent
    ) -> None:
        """Execute one flow: open the app in a browser of its own, then read-decide-act until the
        agent says it is done, and finally judge the declared `expect` against the last state."""
        slug = slugify(check.name)
        self.flow_deadline = min(time.monotonic() + _FLOW_DEADLINE_SECONDS, self.deadline)
        steps: list[str] = []
        history: list[str] = []
        is_finished_by_agent = False

        # A browser of its own per flow, so one flow's cookies and storage never leak into the next.
        browser_reason = await self._start_browser(flow_index)
        if browser_reason:
            await self._finish_flow(
                check, slug, steps, CheckStatus.ERROR, browser_reason, "the box browser never came up"
            )
            return
        endpoint = ui_flows.cdp_endpoint(ui_flows.flow_browser_port(flow_index))

        # The session cookie rides this first request, so the opening navigation is already
        # authenticated rather than bouncing off the proxy's login redirect.
        opening = ui_flows.FlowAction(
            kind=ui_flows.FlowActionKind.OPEN, role="", target="", text=target_url, amount=0, reasoning="open the app"
        )
        outcome = await self._run_step_script(
            ui_flows.build_step_request(
                opening,
                self._flow_screenshot_path(slug, 0),
                cdp_endpoint_url=endpoint,
                preauth_cookie=self.preauth_cookie.get_secret_value(),
                origin=target_url,
            )
        )
        if not outcome.is_ok:
            # The harness's own navigation to the delivered app's forwarded origin. If THIS fails on
            # the instrument, it cannot look at the app at all; if it fails on the app -- a page that
            # never loads -- that is the deliverable falling short.
            # A step that failed always names its layer; a report that names none is the executor
            # failing to say what happened, which is an instrument failure like any other. The
            # status follows the reason, so the two can never disagree.
            reason = outcome.reason or ui_flows.REASON_STEP_ERROR
            await self._finish_flow(
                check,
                slug,
                steps,
                CheckStatus.ERROR if ui_flows.is_instrument_reason(reason) else CheckStatus.FAILED,
                reason,
                outcome.detail,
            )
            return
        state_text = outcome.state_text

        for step_index in range(1, ui_flows.MAX_STEPS_PER_FLOW + 1):
            if self._remaining_seconds <= 0:
                await self._finish_flow(
                    check, slug, steps, CheckStatus.ERROR, REASON_TIMEOUT, "the collection budget ran out mid-flow"
                )
                return
            if time.monotonic() >= self.flow_deadline:
                # The flow's own deadline, unlike the phase budget, is about THIS app: a page that
                # never settles is the delivered thing being unusable.
                await self._finish_flow(
                    check,
                    slug,
                    steps,
                    CheckStatus.FAILED,
                    ui_flows.REASON_FLOW_DEADLINE,
                    "the flow did not finish within its deadline",
                )
                return
            action, _call = agent.decide_next_action(check.steps, tuple(history), state_text)
            if action is None:
                await self._finish_flow(
                    check,
                    slug,
                    steps,
                    CheckStatus.ERROR,
                    ui_flows.REASON_VERIFIER_AGENT_FAILED,
                    "the verification agent returned no usable action",
                )
                return
            described = ui_flows.describe_action(action)
            if action.kind == ui_flows.FlowActionKind.DONE:
                steps.append(
                    ui_flows.flow_step_record(
                        step_index, described, action.reasoning, state_text, "", "", utc_now_iso()
                    )
                )
                is_finished_by_agent = True
                break
            history.append(described)
            outcome = await self._run_step_script(
                ui_flows.build_step_request(
                    action,
                    self._flow_screenshot_path(slug, step_index),
                    cdp_endpoint_url=endpoint,
                    preauth_cookie="",
                    origin=target_url,
                )
            )
            if ui_flows.is_instrument_reason(outcome.reason):
                steps.append(
                    ui_flows.flow_step_record(
                        step_index, described, action.reasoning, state_text, "", outcome.reason, utc_now_iso()
                    )
                )
                await self._finish_flow(check, slug, steps, CheckStatus.ERROR, outcome.reason, outcome.detail)
                return
            step_error = ""
            if not outcome.is_ok:
                # The action did not land but the browser is fine -- an element that is not there,
                # a click that hit nothing. The page below shows the truth, so the flow carries on
                # with the failure recorded where the grade-time judge will read it.
                step_error = _bounded(outcome.detail.strip(), 200)
                history.append("(that action failed: {})".format(step_error))
            steps.append(
                ui_flows.flow_step_record(
                    step_index,
                    described,
                    action.reasoning,
                    state_text,
                    # The executor names the frame it actually wrote, and names nothing when the
                    # capture failed. Naming the file it would have written instead would put a
                    # screenshot that does not exist in front of the grade-time judge.
                    outcome.screenshot_name,
                    step_error,
                    utc_now_iso(),
                )
            )
            state_text = outcome.state_text or state_text

        # The agent's account of the state the flow ended in. Evidence for the judge, never a
        # verdict on the `expect` -- and a call that produced nothing costs the flow its context,
        # not its completion, because the step log already carries every state that was seen.
        reading, _reading_call = agent.read_final_state(check.steps, tuple(history), state_text)
        observation = reading.observation if reading is not None else ""
        steps.append(ui_flows.flow_reading_record(len(steps) + 1, observation, state_text, utc_now_iso()))
        # Completion, not achievement: a flow that carried out its declared steps is `completed`,
        # and one that ran out of budget first is `incomplete`. Whether the app did what the
        # `expect` describes is decided at grade time, from this evidence.
        await self._finish_flow(
            check,
            slug,
            steps,
            CheckStatus.PASSED if is_finished_by_agent else CheckStatus.FAILED,
            "" if is_finished_by_agent else ui_flows.REASON_STEP_BUDGET_EXHAUSTED,
            "expected: {}\nagent's reading of the final state: {}".format(
                check.expect, observation or "(none recorded)"
            ),
        )

    def _flow_screenshot_path(self, slug: str, step_index: int) -> str:
        """Where the step script writes a frame: straight into the box's evidence directory.

        The browser runs in the box, so the screenshot is already where the declared artifact
        collector will find it -- there is no workspace staging leg and no rsync at all, which is
        the transport the fleet executor needed and this one does not.
        """
        return "{}/{}/{}/step_{:03d}.png".format(self._box_dir, FLOWS_DIRNAME, slug, step_index)

    async def _finish_flow(
        self, check: UiFlowCheck, slug: str, steps: Sequence[str], status: CheckStatus, reason: str, detail: str
    ) -> None:
        """Write one flow's step log and record its entry. Screenshots are already in place."""
        await self._write_evidence(
            "{}/{}/{}".format(FLOWS_DIRNAME, slug, FLOW_LOG_FILENAME), "".join(line + "\n" for line in steps)
        )
        self.entries.append(_flow_entry(check, status, reason, _bounded(detail, MAX_COMMAND_OUTPUT_CHARS)))
        await self._flush_record()

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
                        bool(service_states),
                    )
                )
        self._record_phase("app_checks", started_at)


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
        entries=_oracle_entries(expectations),
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
    return "".join(
        line + "\n"
        for line in (
            ui_flows.flow_step_record(
                0,
                "open {}".format(_ORACLE_APP_URL),
                "Opening the delivered app to start the flow.",
                "browser minds-eval-verify @ {}  ({})".format(_ORACLE_APP_URL, check.name),
                "",
                "",
                "1970-01-01T00:00:00+00:00",
            ),
            ui_flows.flow_step_record(
                1,
                "finish the flow",
                "Every declared step has been carried out.",
                "browser minds-eval-verify @ {}  ({})\n{}".format(_ORACLE_APP_URL, check.name, check.expect),
                "",
                "",
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
def _oracle_entries(expectations: ExpandedExpectations) -> tuple[ManifestEntry, ...]:
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
    return tuple(entries)

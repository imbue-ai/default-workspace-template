#!/usr/bin/env python3
"""Resident in-workspace collector for bug-report diagnostics.

Invoked by the minds desktop app via a small ``mngr exec`` as
``python3 system/scripts/collect_bug_report_diagnostics.py [--logs]
[--transcript] [--scan-timeout=<seconds>]``. Stdlib only (no venv, no
third-party imports), targeting the container's system python3 (3.11+).

Prints exactly one line: a JSON object of the shape

    {"contract_version": 1, "zip": "<base64 of a zip>", "omissions": {...}}

``zip`` is absent (not empty) when nothing was collected. The zip holds
``workspace-logs.log`` (when --logs scanned clean) and one
``chats/<agent-id>-<harness>.jsonl`` per selected chat, newest chat first
(when --transcript scanned clean). ``omissions`` explains, per requested
content type, anything that was withheld; a content type that was not
requested appears in neither the zip nor omissions.

Nothing leaves the container unscanned: every chat, the logs text, and each
future zip member's own filename are staged as PLAINTEXT and run through the
template's own secret-scan gate before anything is packed. A scanner pointed
at compressed bytes matches nothing and reports clean, which would turn the
archive into a way around the scan, so the scan always happens first.
"""

import base64
import configparser
from datetime import datetime
import glob
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from collections.abc import Mapping, Sequence

CONTRACT_VERSION = 1

WORKSPACE_DIR = "/home/user/workspace"
# The workspace's own service definitions, and the single source of truth for
# which supervisord programs are user-created; minds must not carry a duplicate
# list of service names.
SUPERVISORD_CONF = WORKSPACE_DIR + "/system/supervisord.conf"
SHED_LEDGER = WORKSPACE_DIR + "/data/.state/oom_priority/events/shed.jsonl"
SUPERVISOR_LOG_DIR = "/var/log/supervisor"

# The template's own secret-scan gate (scan_secrets.sh + its sibling
# betterleaks.toml), shared with the publish-template flow. The scanner
# binaries it drives are baked into the image by install_secret_scanners.sh.
SCAN_GATE_DIR = WORKSPACE_DIR + "/.agents/skills/publish-template/scripts"

# The workspace's own mngr, asked for what agents exist and what was said in
# them. Named here rather than inline so a test can point it at a stub.
MNGR_BINARY = "mngr"

# The kernel's own readings, named here rather than inline so the host-health
# section can be exercised against fixtures.
MEMINFO_PATH = "/proc/meminfo"
UPTIME_PATH = "/proc/uptime"
LOADAVG_PATH = "/proc/loadavg"

# How far back a chat counts as recent: every chat transcript written to inside
# this window is attached, on the view that a bug is rarely about exactly one
# conversation. Outside it, the single most relevant chat still rides along.
TRANSCRIPT_RECENCY_WINDOW_SECONDS = 2 * 60 * 60

WORKSPACE_LOGS_KEY = "workspace_logs"
TRANSCRIPT_KEY = "transcript"

MAX_LOG_FILES = 100
MAX_LINES_PER_LOG = 200
MAX_SHED_LINES = 50
# /proc/meminfo is ~50 lines; this keeps every headline figure and drops the
# hugepage tail.
MAX_MEMINFO_LINES = 40
# Ceiling on how much of any one file is read. supervisord rotates each log at
# 10MB, so reading 100 of them whole would cost a gigabyte for 200 lines each.
MAX_READ_BYTES = 256 * 1024
# Well under the host's collection budget, so a slow scan degrades to
# scanner_unavailable instead of consuming the whole budget. The host overrides
# it via --scan-timeout to keep it the same fraction of whatever budget it runs
# under.
DEFAULT_SCAN_TIMEOUT_SECONDS = 12
SUPERVISORCTL_TIMEOUT_SECONDS = 2
DF_TIMEOUT_SECONDS = 2
GIT_TIMEOUT_SECONDS = 2

# Where each kind of member lives inside the archive.
METADATA_MEMBER_NAME = "metadata.json"
LOG_MEMBER_DIR = "logs"
CHAT_MEMBER_DIR = "chats"

# The window of modification times the zip format can record, as a DOS date
# packing the year into 7 bits from 1980: 1980-01-01 to 2107-12-31, both UTC.
# Member timestamps are clamped into it rather than passed through, because
# either end raises mid-archive and would cost the report every attachment.
ZIP_MIN_TIMESTAMP = 315532800
ZIP_MAX_TIMESTAMP = 4354819199

REASON_SCANNER_UNAVAILABLE = "scanner_unavailable"
REASON_SECRETS_FOUND = "secrets_found"
REASON_NO_CHAT_TRANSCRIPT = "no_chat_transcript"

FINDING_MARKER = "SECRET SCAN FINDING"
# Every marker scan_secrets.sh prints for "one of my two mandatory scanners did
# not run to completion": a target/scanner/config precondition it refused to
# scan without, and a scanner that exited outside its clean/findings codes. Any
# of them means nothing was scanned by both scanners, so no target is clean --
# even when the other scanner printed findings for some *other* file.
SCANNER_MALFUNCTION_MARKERS = (
    "TARGET MISSING:",
    "SCANNER MISSING:",
    "CONFIG MISSING:",
    "SCANNER ERROR:",
)
# A scanner exited with its findings code but its report could not be parsed, so
# there are leaks that name no file: every target has to wear them.
UNPARSEABLE_REPORT_MARKER = "SECRET SCAN FAILED"


def read_bounded(path: str, from_end: bool) -> str:
    """At most MAX_READ_BYTES of a file, taken from either its start or its end."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if from_end and size > MAX_READ_BYTES:
                fh.seek(size - MAX_READ_BYTES)
            data = fh.read(MAX_READ_BYTES)
    except OSError as e:
        return "(unreadable: {!r})".format(e)
    return data.decode("utf-8", errors="replace")


def read_tail(path: str, max_lines: int) -> str:
    """Last max_lines lines of a file. For append-ordered files, where the end is the news."""
    return "\n".join(read_bounded(path, True).splitlines()[-max_lines:])


def read_head(path: str, max_lines: int) -> str:
    """First max_lines lines of a file. For files whose headline values come first."""
    return "\n".join(read_bounded(path, False).splitlines()[:max_lines])


def run_command(argv: Sequence[str], timeout: float) -> str:
    """Combined stdout+stderr of a command, or a note when it could not run."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        return "(failed: {!r})".format(e)
    return (proc.stdout + proc.stderr).strip()


def safe_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def load_user_program_names() -> set[str] | None:
    """Names of the supervisord programs the workspace marks as user-created, or None.

    Every service the workspace starts is wrapped in ``oom_tag_service.py <band>``,
    and a user-created app is the one passing the literal band ``user`` -- that
    argument is how the workspace itself distinguishes its own apps, so it is read
    here rather than guessed at.

    The alternative, asking ``oom_priority.bands`` for each program's band, answers
    a different question: that table ranks what is expendable under memory
    pressure, and anything absent from it falls back to the user band. Built-in
    services that predate the table or skip the wrapper entirely (``cron``) would
    be read as user apps and their logs silently dropped.

    None means the config could not be read, which the caller reports rather than
    guessing a classification from.
    """
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        if not parser.read(SUPERVISORD_CONF, encoding="utf-8"):
            return None
    except (OSError, configparser.Error):
        return None
    user_programs = set()
    for section in parser.sections():
        if not section.startswith("program:"):
            continue
        command = parser.get(section, "command", fallback="")
        if re.search(r"oom_tag_service\.py\s+user(\s|$)", command):
            user_programs.add(section[len("program:") :])
    return user_programs


def program_name_for_log(path: str) -> str:
    """The supervisord program a log file belongs to, from its filename."""
    name = os.path.basename(path)
    for suffix in ("-stderr.log", "-stdout.log"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def select_log_files(user_programs: set[str] | None) -> list[str]:
    """Supervisord log files worth sending, newest first and capped.

    A user app's logs are workspace content rather than diagnostics, so they never
    leave the container. ``user_programs`` of None means the classification could
    not be made at all, and nothing is filtered -- the caller says so in the
    payload rather than letting the omission pass unremarked.
    """
    candidates = list(glob.glob(SUPERVISOR_LOG_DIR + "/*-stderr.log"))
    system_interface_stdout = SUPERVISOR_LOG_DIR + "/system_interface-stdout.log"
    if os.path.exists(system_interface_stdout):
        candidates.append(system_interface_stdout)
    if user_programs is not None:
        candidates = [
            p for p in candidates if program_name_for_log(p) not in user_programs
        ]
    candidates.sort(key=safe_mtime, reverse=True)
    return candidates[:MAX_LOG_FILES]


def build_metadata() -> dict[str, object]:
    """The report's structured context: what this workspace is and how it is doing.

    A json member rather than headed sections in a text blob, so a reader (or a
    tool) gets fields instead of something to re-parse. Values that are
    inherently command output -- df, meminfo, supervisorctl -- stay as their own
    string fields rather than being given an invented schema; what matters is
    that they are separately addressable instead of concatenated together.
    """
    user_programs = load_user_program_names()
    return {
        "workspace": {
            "commit": run_command(
                ["git", "-C", WORKSPACE_DIR, "log", "-1", "--format=%H %cI %s"],
                GIT_TIMEOUT_SECONDS,
            ),
            "branch": run_command(
                ["git", "-C", WORKSPACE_DIR, "rev-parse", "--abbrev-ref", "HEAD"],
                GIT_TIMEOUT_SECONDS,
            ),
            "local_changes": run_command(
                ["git", "-C", WORKSPACE_DIR, "status", "--short"], GIT_TIMEOUT_SECONDS
            )
            or "(clean)",
        },
        "host_health": {
            "disk": run_command(["df", "-h"], DF_TIMEOUT_SECONDS),
            # meminfo is head-ordered: MemTotal/MemFree/MemAvailable/Buffers/Cached
            # lead the file, and the tail is Hugetlb/Vmalloc detail nobody reads.
            "memory": read_head(MEMINFO_PATH, MAX_MEMINFO_LINES),
            "uptime_seconds_up_and_idle": read_head(UPTIME_PATH, 2),
            "loadavg": read_head(LOADAVG_PATH, 2),
            "recent_memory_shed_events": read_tail(SHED_LEDGER, MAX_SHED_LINES),
        },
        "services": {
            "status": run_command(
                ["supervisorctl", "-c", SUPERVISORD_CONF, "status"],
                SUPERVISORCTL_TIMEOUT_SECONDS,
            ),
            # None means the config could not be read, so user-created services
            # could not be told apart and every log was collected. Recorded
            # rather than silent: it changes what the log members mean.
            "are_user_services_identified": user_programs is not None,
            "log_lines_per_file": MAX_LINES_PER_LOG,
        },
    }


def collect_log_members() -> list[tuple[str, str, float]]:
    """One member per service log, as ``(member name, content, mtime)``.

    Separate members rather than one concatenated file: the payload is an
    archive, so there is no reason to make a reader split headed sections apart
    again. User-created services' logs are excluded -- those are the user's
    content, not diagnostics -- by the workspace's own service definitions.
    """
    members = []
    used_names: set[str] = set()
    for path in select_log_files(load_user_program_names()):
        stem = safe_member_component(program_name_for_log(path))
        member = "{}/{}.log".format(LOG_MEMBER_DIR, stem)
        index = 2
        while member in used_names:
            member = "{}/{}-{}.log".format(LOG_MEMBER_DIR, stem, index)
            index += 1
        used_names.add(member)
        members.append((member, read_tail(path, MAX_LINES_PER_LOG), safe_mtime(path)))
    return members


def safe_member_component(text: str) -> str:
    """One path component reduced to characters that are safe to extract from a zip.

    Member names are built from directory names inside the workspace, so they
    reach a reader's filesystem on extraction. Anything outside a conservative
    set becomes an underscore and leading dots are dropped, so no member can
    name a traversal (``..``) or a hidden file.
    """
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", text).lstrip(".")
    # A dotted run cannot traverse without a separator, which the substitution
    # above already removed -- collapsed anyway so no member name can read as
    # a relative path at a glance.
    return cleaned.replace("..", "_") or "unknown"


def transcript_member_name(name: str, harness: str, used_names: set[str]) -> str:
    """The zip member name for one chat, unique within the archive.

    Carries the agent's name and the harness that wrote it, under the fixed ``chats/``
    directory, so the conversations stay tellable apart with nothing injected
    into the transcript itself. The member keeps the harness's own .jsonl, so it
    opens in whatever reads a transcript normally.
    """
    stem = "{}-{}".format(safe_member_component(name), safe_member_component(harness))
    member = "{}/{}.jsonl".format(CHAT_MEMBER_DIR, stem)
    index = 2
    while member in used_names:
        member = "{}/{}-{}.jsonl".format(CHAT_MEMBER_DIR, stem, index)
        index += 1
    used_names.add(member)
    return member


def run_mngr(args: Sequence[str], timeout: float) -> str | None:
    """Stdout of a ``mngr`` subcommand, or None when it could not be run.

    The workspace's own mngr is the source of truth for what agents exist and
    what was said in them, so the collector asks it rather than re-deriving
    either from the files under ~/.mngr. Any failure returns None and the
    caller reports no transcript: a collector that guessed at mngr's state
    would be the duplicate this exists to avoid.
    """
    try:
        proc = subprocess.run(
            [MNGR_BINARY, *args], capture_output=True, text=True, timeout=timeout
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def list_chat_agents(timeout: float) -> list[str]:
    """Each chat agent's name, by the workspace's own definition.

    Mirrors system_interface's get_chat_agent_ids: a chat is any agent that is
    neither a worker another agent spawned (``agent_created=true`` -- caretaker
    runs, automations) nor the primary services agent (``is_primary=true``).
    Those two marks are how the workspace tells its background agents apart, so
    they are read rather than a list of agent types being kept here.

    The pipe-delimited template is used rather than ``--format json``: inside a
    workspace container mngr cannot reach the providers that back its hosts, and
    the json path fails outright on that where the template still answers from
    local state.
    """
    listed = run_mngr(
        [
            "list",
            "--format",
            "{name}|{type}|{labels.is_primary}|{labels.agent_created}",
        ],
        timeout,
    )
    if listed is None:
        return []
    agents: list[str] = []
    for line in listed.splitlines():
        parts = line.split("|")
        if len(parts) != 4:
            continue
        name, agent_type, is_primary, agent_created = (p.strip() for p in parts)
        if not name or not agent_type:
            continue
        if is_primary.lower() == "true" or agent_created.lower() == "true":
            continue
        agents.append(name)
    return agents


def fetch_transcript(name: str, timeout: float) -> str | None:
    """One agent's conversation as raw JSONL, or None when it has none.

    The harness is NOT derived from the agent's type: an agent of type ``chat``
    writes its events under ``claude/``, so the two do not map onto each other.
    Instead mngr is asked for every source and filtered on the source each event
    carries, which keeps the set of harnesses mngr's business rather than a list
    kept here.

    ``logs/`` is excluded deliberately: everything under it is the converter's
    own stdout -- it records *that* it converted, not what was said -- so
    including it would attach a log of conversions in place of the conversation.
    """
    events = run_mngr(
        [
            "event",
            name,
            "--include",
            'source.endsWith("common_transcript")',
            "--exclude",
            'source.startsWith("logs/")',
            "--format",
            "jsonl",
        ],
        timeout,
    )
    if events is None or not events.strip():
        return None
    return events


def transcript_source(events: str) -> str:
    """The harness that wrote these events (``claude``, ``codex``, ...).

    Taken from the events' own ``source`` field rather than the agent's type,
    which does not name it: a ``chat`` agent's events live under ``claude/``.
    """
    for line in events.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            source = json.loads(line).get("source")
        except ValueError:
            continue
        if isinstance(source, str) and "/" in source:
            return source.split("/", 1)[0]
    return "chat"


def newest_event_time(events: str) -> float:
    """When this conversation was last written to, as an epoch; 0.0 when unknown.

    Read from the events themselves rather than mngr's ``user_activity_time``,
    which is unpopulated on the agents this runs against, and rather than a file
    mtime, which the collector no longer resolves paths for.
    """
    newest = 0.0
    for line in events.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            stamp = json.loads(line).get("timestamp")
        except ValueError:
            continue
        if not isinstance(stamp, str):
            continue
        try:
            parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            continue
        newest = max(newest, parsed.timestamp())
    return newest


def collect_transcript_members(timeout: float) -> list[tuple[str, str, float]]:
    """The chats to attach, as ``(member name, content, last-written epoch)``.

    A bug is rarely about exactly one conversation, so every chat written to
    inside the recency window rides along, newest first. When the window is
    empty -- the workspace sat idle -- the report still carries its best single
    context rather than nothing, since a stale workspace is exactly where the
    conversation is hardest to reconstruct from anything else.
    """
    fetched = []
    used_names: set[str] = set()
    for name in list_chat_agents(timeout):
        events = fetch_transcript(name, timeout)
        if events is None:
            continue
        member = transcript_member_name(name, transcript_source(events), used_names)
        fetched.append((member, events, newest_event_time(events)))
    if not fetched:
        return []
    fetched.sort(key=lambda item: item[2], reverse=True)
    cutoff = time.time() - TRANSCRIPT_RECENCY_WINDOW_SECONDS
    recent = [item for item in fetched if item[2] >= cutoff]
    return recent if recent else fetched[:1]

def build_zip(members: Sequence[tuple[str, str, float]]) -> bytes:
    """Deflate the members into one archive, returned as raw zip bytes.

    Only ever called with members the secret scan already cleared. The scan has
    to read the plaintext, never this archive: a scanner pointed at a zip reads
    compressed bytes, matches none of its patterns, and reports the file clean,
    which would turn the archive into a way to smuggle out exactly what the
    scan exists to catch.

    Each member keeps its own source's last-modified time -- in UTC, and
    clamped into the window the format can record -- so a reader sees when each
    conversation was last written without a separate manifest file.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content, mtime in members:
            clamped = min(max(mtime, ZIP_MIN_TIMESTAMP), ZIP_MAX_TIMESTAMP)
            info = zipfile.ZipInfo(name, date_time=time.gmtime(clamped)[:6])
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, content)
    return buffer.getvalue()


def encode_zip(
    log_members: Sequence[tuple[str, str, float]],
    chat_members: Sequence[tuple[str, str, float]],
) -> str | None:
    """Everything collected, packed and base64-encoded; None when there is nothing.

    Deliberately unbounded. The payload returns on the collector's stdout, which
    carries it whole -- 32MB was measured arriving intact -- so there is no
    transport cliff to stay under, and S3 does not care either. What a large
    payload costs is time (roughly 0.45s per MB on top of a ~4s floor) against
    the host's collection budget, and a collection that outgrows that budget
    fails loudly as ``exec_timeout`` rather than quietly shipping a trimmed set
    a reader could not tell from a complete one.
    """
    members = list(log_members) + list(chat_members)
    if not members:
        return None
    return base64.b64encode(build_zip(members)).decode("ascii")


def scan_targets(
    target_paths: Sequence[str], timeout_seconds: float
) -> dict[str, str | None]:
    """Run the template's secret-scan gate over the staged files; path -> reason or None.

    One invocation covers every file (several would multiply the scanners'
    startup cost against a budget that cannot afford it) and findings are
    attributed back to a file by the path in scan_secrets.sh's own finding
    lines, which print a file target exactly as it was passed.

    scan_secrets.sh exits 1 both for findings and for a scanner it could not run,
    and the two can happen in the same run: one scanner can break while the other
    flags a different file. So a malfunction marker is checked before any finding
    line is read, and it disqualifies every target -- a file that only one of the
    two mandatory scanners looked at has not been scanned. Anything else
    ambiguous -- findings that match no target, or a nonzero exit with no marker
    and no findings (including the gate script itself being absent) -- also drops
    every file: a file must only be released on positive evidence that it is clean.
    """
    script = os.path.join(SCAN_GATE_DIR, "scan_secrets.sh")
    config = os.path.join(SCAN_GATE_DIR, "betterleaks.toml")
    argv = ["bash", script, "--config", config] + list(target_paths)
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout_seconds
        )
    except Exception:
        return {path: REASON_SCANNER_UNAVAILABLE for path in target_paths}
    if proc.returncode == 0:
        return {path: None for path in target_paths}
    # scan_secrets.sh reports on stderr, but stdout is folded in so that a
    # failure printed anywhere still counts against the scan.
    output = proc.stdout + "\n" + proc.stderr
    if any(marker in output for marker in SCANNER_MALFUNCTION_MARKERS):
        return {path: REASON_SCANNER_UNAVAILABLE for path in target_paths}
    if UNPARSEABLE_REPORT_MARKER in output:
        return {path: REASON_SECRETS_FOUND for path in target_paths}
    finding_lines = [line for line in output.splitlines() if FINDING_MARKER in line]
    if not finding_lines:
        return {path: REASON_SCANNER_UNAVAILABLE for path in target_paths}
    verdicts = {}
    for path in target_paths:
        matched = [line for line in finding_lines if path in line]
        verdicts[path] = REASON_SECRETS_FOUND if matched else None
    unattributed = [
        line for line in finding_lines if not any(path in line for path in target_paths)
    ]
    if unattributed:
        return {path: REASON_SECRETS_FOUND for path in target_paths}
    return verdicts


def stage_for_scan(staging_dir: str, key: str, content: str) -> str | None:
    """Write one payload to a temp path for scanning; None when it cannot be written."""
    path = os.path.join(staging_dir, "bug-report-{}.staged".format(key))
    try:
        with open(path, "w", encoding="utf-8", errors="replace") as fh:
            fh.write(content)
    except OSError:
        return None
    return path


def parse_scan_timeout(flags: Sequence[str]) -> float:
    """The host's --scan-timeout=<seconds> override, or the default when absent or unusable."""
    for flag in flags:
        if flag.startswith("--scan-timeout="):
            try:
                return float(flag.split("=", 1)[1])
            except ValueError:
                return DEFAULT_SCAN_TIMEOUT_SECONDS
    return DEFAULT_SCAN_TIMEOUT_SECONDS


def main(argv: Sequence[str]) -> None:
    flags = set(argv)
    scan_timeout_seconds = parse_scan_timeout(list(flags))
    omissions: dict[str, str] = {}

    log_members: list[tuple[str, str, float]] = []
    if "--logs" in flags:
        log_members = [
            (METADATA_MEMBER_NAME, json.dumps(build_metadata(), indent=2), time.time()),
            *collect_log_members(),
        ]

    chat_members: list[tuple[str, str, float]] = []
    if "--transcript" in flags:
        chat_members = collect_transcript_members(scan_timeout_seconds)
        if not chat_members:
            omissions[TRANSCRIPT_KEY] = REASON_NO_CHAT_TRANSCRIPT

    # Nothing leaves the container unscanned, so a payload that cannot even be
    # staged for the scanner is dropped exactly as one the scanner could not
    # read. Each chat stages one plaintext file, because the scanner has to
    # read the conversations themselves -- it cannot see inside the archive
    # they are packed into afterwards. Every staged file leads with the zip
    # member name it will be packed under: the name is written into the
    # archive's directory in plaintext, it is built from a directory name
    # inside the workspace, and the sanitizer that shapes it keeps every
    # character a credential is written with.
    staging_dir = tempfile.mkdtemp(prefix="bug-report-scan-")
    try:
        staged_by_key: dict[str, list[str]] = {}
        if log_members:
            logs_staged: list[str] | None = []
            for index, (name, content, _) in enumerate(log_members):
                staged = stage_for_scan(
                    staging_dir,
                    "{}-{}".format(WORKSPACE_LOGS_KEY, index),
                    name + "\n" + content,
                )
                if staged is None:
                    logs_staged = None
                    break
                logs_staged.append(staged)
            if logs_staged is None:
                log_members = []
                omissions[WORKSPACE_LOGS_KEY] = REASON_SCANNER_UNAVAILABLE
            else:
                staged_by_key[WORKSPACE_LOGS_KEY] = logs_staged
        if chat_members:
            transcript_staged: list[str] | None = []
            for index, (name, content, _) in enumerate(chat_members):
                staged = stage_for_scan(
                    staging_dir,
                    "{}-{}".format(TRANSCRIPT_KEY, index),
                    name + "\n" + content,
                )
                if staged is None:
                    transcript_staged = None
                    break
                transcript_staged.append(staged)
            if transcript_staged is None:
                chat_members = []
                omissions[TRANSCRIPT_KEY] = REASON_SCANNER_UNAVAILABLE
            else:
                staged_by_key[TRANSCRIPT_KEY] = transcript_staged

        if staged_by_key:
            targets = [path for paths in staged_by_key.values() for path in paths]
            verdicts = scan_targets(targets, scan_timeout_seconds)
            for key, paths in staged_by_key.items():
                # An attachment made of several files is released only when
                # every one of them is clean: one chat carrying a secret
                # withholds the whole archive rather than quietly shipping a
                # partial set of conversations, which a reader could not tell
                # from the full set.
                reasons = [
                    verdicts.get(path, REASON_SCANNER_UNAVAILABLE) for path in paths
                ]
                reason = next((r for r in reasons if r is not None), None)
                if reason is not None:
                    if key == WORKSPACE_LOGS_KEY:
                        log_members = []
                    if key == TRANSCRIPT_KEY:
                        chat_members = []
                    omissions[key] = reason
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)

    # Base64 because JSON holds no bytes; the host decodes it back and stages
    # the archive verbatim. "zip" is absent -- never empty -- when nothing was
    # collected.
    encoded = encode_zip(log_members, chat_members)
    payload: dict[str, object] = {
        "contract_version": CONTRACT_VERSION,
        "omissions": omissions,
    }
    if encoded is not None:
        payload["zip"] = encoded
    print(json.dumps(payload))


if __name__ == "__main__":
    main(sys.argv[1:])

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
# betterleaks.toml), shared with the publish-inspiration flow. The scanner
# binaries it drives are baked into the image by install_secret_scanners.sh.
SCAN_GATE_DIR = WORKSPACE_DIR + "/.agents/skills/publish-inspiration/scripts"

# The kernel's own readings, named here rather than inline so the host-health
# section can be exercised against fixtures.
MEMINFO_PATH = "/proc/meminfo"
UPTIME_PATH = "/proc/uptime"
LOADAVG_PATH = "/proc/loadavg"

# mngr's per-agent state, where every agent's common transcript is written. The
# exec runs as root, so "~" is /root rather than the workspace user's home; the
# explicit path is tried first and the expansions are fallbacks for a workspace
# laid out differently.
MNGR_HOME_CANDIDATES = [
    "/home/user/.mngr",
    os.environ.get("MNGR_HOST_DIR", ""),
    os.path.expanduser("~/.mngr"),
]

# Event-source directory names under an agent's events/ that are not a harness
# transcript. ``logs`` is the common-transcript converter's own stdout stream.
NON_HARNESS_EVENT_SOURCES = ("logs",)

# How far back a chat counts as recent: every chat transcript written to inside
# this window is attached, on the view that a bug is rarely about exactly one
# conversation. Outside it, the single most relevant chat still rides along.
TRANSCRIPT_RECENCY_WINDOW_SECONDS = 2 * 60 * 60

MAX_LOG_FILES = 100
MAX_LINES_PER_LOG = 200
MAX_SHED_LINES = 50
# /proc/meminfo is ~50 lines; this keeps every headline figure and drops the
# hugepage tail.
MAX_MEMINFO_LINES = 40
# Ceiling on how much of any one file is read. supervisord rotates each log at
# 10MB, so reading 100 of them whole would cost a gigabyte for 200 lines each.
MAX_READ_BYTES = 256 * 1024
# Ceiling on the base64 zip payload the JSON line carries. Over it, the oldest
# chats are dropped first and the drop is recorded inside the zip itself (see
# DROPPED_CHATS_MEMBER_NAME) so a reader can tell a trimmed set from a full one.
MAX_ENCODED_ZIP_BYTES = 8 * 1024 * 1024
# Well under the collection budget, so a slow scan degrades to
# scanner_unavailable instead of timing out the whole exec. The host overrides
# this via --scan-timeout to keep it the same fraction of whatever budget it is
# running under (a test budget on a slow CI sandbox is several times longer).
DEFAULT_SCAN_TIMEOUT_SECONDS = 12
SUPERVISORCTL_TIMEOUT_SECONDS = 2
DF_TIMEOUT_SECONDS = 2
GIT_TIMEOUT_SECONDS = 2

WORKSPACE_LOGS_KEY = "workspace_logs"
TRANSCRIPT_KEY = "transcript"

WORKSPACE_LOGS_MEMBER_NAME = "workspace-logs.log"
CHAT_MEMBER_DIR = "chats"
# The note member recording chats dropped for size. Its prose is authored here
# and the member names it lists were each scanned alongside their chat, so it
# carries nothing the scanner did not read.
DROPPED_CHATS_MEMBER_NAME = "DROPPED-CHATS.txt"
# In-band marker for a logs member that lost its older half to the size cap,
# and the floor below which no further halving is attempted.
LOGS_TRUNCATION_NOTE = (
    "(truncated to fit the bug-report size cap; older lines dropped)\n"
)
MIN_TRUNCATED_LOGS_BYTES = 1024

# The window of modification times the zip format can record, as a DOS date
# packing the year into 7 bits from 1980: 1980-01-01 to 2107-12-31, both UTC.
# Member timestamps are clamped into it rather than passed through, because
# either end raises mid-archive and would cost the report every attachment. A
# transcript whose mtime could not be read falls back to 0 (the 1970 epoch) and
# a clock-skewed or bogusly touched file can sit past 2107, so both ends are
# reachable. Read back with time.gmtime, never localtime: the lower bound is
# 1979 in every timezone behind UTC, which the format rejects just the same.
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


def build_host_health() -> str:
    """Disk, memory, uptime/load, and the recent memory-shed events.

    Prepended to the log bundle because the questions a bug report raises about a
    slow or half-dead workspace ("did it run out of disk? was something shed?")
    are answered by these four readings, not by any one service's log.
    """
    sections = [
        "--- df -h ---\n" + run_command(["df", "-h"], DF_TIMEOUT_SECONDS),
        # meminfo is head-ordered: MemTotal/MemFree/MemAvailable/Buffers/Cached
        # lead the file, and the tail is Hugetlb/Vmalloc detail nobody reads.
        "--- {} ---\n".format(MEMINFO_PATH)
        + read_head(MEMINFO_PATH, MAX_MEMINFO_LINES),
        "--- {} (seconds up, seconds idle) ---\n".format(UPTIME_PATH)
        + read_head(UPTIME_PATH, 2),
        "--- {} ---\n".format(LOADAVG_PATH) + read_head(LOADAVG_PATH, 2),
        "--- recent memory-shed events ({}) ---\n".format(SHED_LEDGER)
        + read_tail(SHED_LEDGER, MAX_SHED_LINES),
    ]
    return "\n\n".join(sections)


def build_workspace_version() -> str:
    """The workspace checkout's own version: commit, branch, and any local drift.

    The report's basics carry the desktop app's version, but the bug may live in
    the workspace template itself, and the two move independently. Outside a git
    checkout this degrades to git's own error text -- still worth attaching,
    since "the workspace is not a git repo" is itself diagnostic.
    """
    return "\n".join(
        [
            run_command(
                ["git", "-C", WORKSPACE_DIR, "log", "-1", "--format=%H %cI %s"],
                GIT_TIMEOUT_SECONDS,
            ),
            run_command(
                ["git", "-C", WORKSPACE_DIR, "rev-parse", "--abbrev-ref", "HEAD"],
                GIT_TIMEOUT_SECONDS,
            ),
            run_command(
                ["git", "-C", WORKSPACE_DIR, "status", "--short"], GIT_TIMEOUT_SECONDS
            )
            or "(clean)",
        ]
    )


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


def build_workspace_logs() -> str:
    """The whole workspace-logs payload: host health, service status, service logs."""
    user_programs = load_user_program_names()
    parts = ["=== workspace version ===", build_workspace_version(), ""]
    parts.extend(["=== host health ===", build_host_health(), ""])
    if user_programs is None:
        parts.append(
            "=== NOTE ===\n"
            "Could not read {}, so user-created services could not be identified\n"
            "and every supervisord log below is included.".format(SUPERVISORD_CONF)
        )
        parts.append("")
    parts.append("=== supervisorctl status ===")
    parts.append(
        run_command(
            ["supervisorctl", "-c", SUPERVISORD_CONF, "status"],
            SUPERVISORCTL_TIMEOUT_SECONDS,
        )
    )
    parts.append("")
    log_files = select_log_files(user_programs)
    parts.append(
        "=== {} service log files (last {} lines each) ===".format(
            len(log_files), MAX_LINES_PER_LOG
        )
    )
    for path in log_files:
        parts.append("")
        parts.append("--- {} ---".format(path))
        parts.append(read_tail(path, MAX_LINES_PER_LOG))
    return "\n".join(parts)


def agent_labels_for_transcript(path: str) -> dict[str, object]:
    """The owning agent's labels, from its data.json; {} when unreadable.

    The transcript sits at agents/<id>/events/<source>/common_transcript/
    events.jsonl, so the agent's record is four directories up. An agent whose
    record cannot be read carries no marks, and no marks reads as a chat below
    -- old workspaces without labels keep working.
    """
    agent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(path))))
    try:
        with open(os.path.join(agent_dir, "data.json"), encoding="utf-8") as fh:
            record = json.load(fh)
    except (OSError, ValueError):
        return {}
    labels = record.get("labels") if isinstance(record, dict) else None
    return labels if isinstance(labels, dict) else {}


def is_chat_agent(labels: Mapping[str, object]) -> bool:
    """Whether these labels mark a chat agent, by the workspace's own definition.

    Mirrors system_interface's get_chat_agent_ids: a chat is any agent that is
    neither a worker another agent spawned (``agent_created=true`` -- caretaker
    runs, automations) nor the primary services agent (``is_primary=true``).
    Those two marks are how the workspace itself tells its background agents
    apart, so they are read here rather than a list of agent types being kept.
    """
    return labels.get("agent_created") != "true" and labels.get("is_primary") != "true"


def last_user_message_timestamp(path: str) -> str | None:
    """Timestamp of the transcript tail's last user message, or None when it has none.

    When the user last spoke is a far better recency signal than the file's
    mtime: a background chat's assistant can churn out events long after the
    user stopped looking at it, and every one of them bumps the mtime. Only the
    tail is scanned (the same byte-bounded read the log tails use), so a user
    message older than the tail window reads as none -- by then the transcript's
    recent activity is all non-user anyway, which is exactly the signal.
    Timestamps are compared as strings: common-transcript events carry ISO-8601
    UTC, where lexicographic order is time order.
    """
    for line in reversed(read_bounded(path, True).splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "user_message":
            timestamp = event.get("timestamp")
            return timestamp if isinstance(timestamp, str) else ""
    return None


def gather_chat_transcripts() -> list[tuple[str, float]]:
    """Every chat agent's transcript as (path, mtime), deduplicated.

    Only chat agents' transcripts are candidates (see ``is_chat_agent``): a
    workspace whose only transcripts belong to background agents has no chat
    transcript to send. The source segment names the harness whose transcript
    this is (claude, codex, ...), except for ``logs``, which is the converter's
    own stdout stream -- it records that it converted, not what was said -- and
    is never a candidate.
    """
    seen = set()
    candidates = []
    for root in MNGR_HOME_CANDIDATES:
        if not root:
            continue
        pattern = os.path.join(
            root, "agents", "*", "events", "*", "common_transcript", "events.jsonl"
        )
        for path in glob.glob(pattern):
            if (
                os.path.basename(os.path.dirname(os.path.dirname(path)))
                in NON_HARNESS_EVENT_SOURCES
            ):
                continue
            real = os.path.realpath(path)
            if real in seen:
                continue
            seen.add(real)
            if not is_chat_agent(agent_labels_for_transcript(path)):
                continue
            candidates.append((path, safe_mtime(path)))
    return candidates


def select_transcript_paths() -> list[str]:
    """The chat transcripts a report should carry, newest first; [] when none exist.

    A bug is rarely about exactly one conversation, so every chat written to
    inside the recency window rides along, ordered newest first. When the
    window is empty -- the workspace has sat idle -- the report still carries
    its best single context: the chat the user most recently wrote in, ranked
    by the last user message (a background chat's assistant churning does not
    move that signal) with the file mtime breaking ties and standing in for
    transcripts holding no user message.
    """
    candidates = gather_chat_transcripts()
    cutoff = time.time() - TRANSCRIPT_RECENCY_WINDOW_SECONDS
    recent = sorted(
        (c for c in candidates if c[1] >= cutoff), key=lambda c: c[1], reverse=True
    )
    if recent:
        return [path for path, _ in recent]
    best = None
    best_rank = None
    for path, mtime in candidates:
        user_timestamp = last_user_message_timestamp(path)
        rank = (user_timestamp is not None, user_timestamp or "", mtime)
        if best_rank is None or rank > best_rank:
            best, best_rank = path, rank
    return [best] if best is not None else []


def read_transcript(path: str) -> str | None:
    """The transcript's raw JSONL, or None when it cannot be read."""
    try:
        with open(path, "rb") as fh:
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None


def safe_member_component(text: str) -> str:
    """One path component reduced to characters that are safe to extract from a zip.

    Member names are built from directory names inside the workspace, so they
    reach a reader's filesystem on extraction. Anything outside a conservative
    set becomes an underscore and leading dots are dropped, so no member can
    name a traversal (``..``) or a hidden file.
    """
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", text).lstrip(".")
    return cleaned or "unknown"


def transcript_member_name(path: str, used_names: set[str]) -> str:
    """The zip member name for one chat transcript, unique within the archive.

    Carries the owning agent id and the harness source under the fixed
    ``chats/`` directory, so the conversations stay tellable apart with nothing
    injected into the transcript itself. The member keeps the harness's own
    .jsonl, so it opens in whatever reads a transcript normally.
    """
    agent_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(path))))
    source = os.path.basename(os.path.dirname(os.path.dirname(path)))
    stem = "{}-{}".format(
        safe_member_component(os.path.basename(agent_dir)),
        safe_member_component(source),
    )
    name = "{}/{}.jsonl".format(CHAT_MEMBER_DIR, stem)
    index = 2
    while name in used_names:
        name = "{}/{}-{}.jsonl".format(CHAT_MEMBER_DIR, stem, index)
        index += 1
    used_names.add(name)
    return name


def collect_transcript_members(paths: Sequence[str]) -> list[tuple[str, str, float]]:
    """The chat transcripts to attach, as (member name, content, mtime) triples.

    Each chat stays its own file: the attachment is an archive, so there is no
    reason to flatten several JSONL streams into one blob a reader has to split
    back apart. Order follows ``select_transcript_paths`` (newest chat first).
    A transcript that cannot be read is skipped, and an empty result is the
    no-transcript answer.
    """
    members = []
    used_names = set()
    for path in paths:
        content = read_transcript(path)
        if content is None:
            continue
        members.append(
            (transcript_member_name(path, used_names), content, safe_mtime(path))
        )
    return members


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


def build_dropped_chats_note(dropped_names: Sequence[str]) -> str:
    return (
        "The following chat transcripts were collected and scanned clean, but were\n"
        "dropped (oldest first) to keep the bug-report payload under its size cap:\n\n"
        + "\n".join(dropped_names)
        + "\n"
    )


def encode_zip_capped(
    logs_member: tuple[str, str, float] | None,
    chat_members: Sequence[tuple[str, str, float]],
) -> tuple[str | None, list[str]]:
    """The base64 zip payload under the size cap, plus the chat members dropped to fit.

    Oldest chats go first (they are the least likely to describe the bug), and
    every drop is recorded inside the zip as a note member -- a silently trimmed
    set would be indistinguishable from a complete one, the same reason one
    secret finding withholds all chats. If the logs alone still bust the cap
    (only reachable when many services tail incompressible garbage), the logs
    text keeps losing its older half until it fits -- truncating text the scan
    already cleared cannot introduce anything unscanned, and the loss is marked
    in-band at the top of the member. None means there was nothing to pack.
    """
    kept_chats = list(chat_members)
    dropped_names: list[str] = []
    while True:
        members = ([] if logs_member is None else [logs_member]) + kept_chats
        if dropped_names:
            members = members + [
                (
                    DROPPED_CHATS_MEMBER_NAME,
                    build_dropped_chats_note(dropped_names),
                    time.time(),
                )
            ]
        if not members:
            return None, dropped_names
        encoded = base64.b64encode(build_zip(members)).decode("ascii")
        if len(encoded) <= MAX_ENCODED_ZIP_BYTES:
            return encoded, dropped_names
        if kept_chats:
            # Chats are ordered newest first, so the oldest is the last one kept.
            dropped_names.append(kept_chats.pop()[0])
        elif (
            logs_member is not None and len(logs_member[1]) >= MIN_TRUNCATED_LOGS_BYTES
        ):
            # Logs are append-ordered, so the older half is the expendable one.
            name, content, mtime = logs_member
            logs_member = (
                name,
                LOGS_TRUNCATION_NOTE + content[len(content) // 2 :],
                mtime,
            )
        else:
            # Nothing left to shrink; effectively unreachable (a zip of a few
            # hundred text bytes cannot exceed the cap) but ships rather than
            # loops.
            return encoded, dropped_names


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

    logs_text = build_workspace_logs() if "--logs" in flags else None

    chat_members: list[tuple[str, str, float]] = []
    if "--transcript" in flags:
        chat_members = collect_transcript_members(select_transcript_paths())
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
        if logs_text is not None:
            logs_staged = stage_for_scan(
                staging_dir,
                WORKSPACE_LOGS_KEY,
                WORKSPACE_LOGS_MEMBER_NAME + "\n" + logs_text,
            )
            if logs_staged is None:
                logs_text = None
                omissions[WORKSPACE_LOGS_KEY] = REASON_SCANNER_UNAVAILABLE
            else:
                staged_by_key[WORKSPACE_LOGS_KEY] = [logs_staged]
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
                        logs_text = None
                    if key == TRANSCRIPT_KEY:
                        chat_members = []
                    omissions[key] = reason
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)

    # Base64 because JSON holds no bytes; the host decodes it back and stages
    # the archive verbatim. "zip" is absent -- never empty -- when nothing was
    # collected.
    logs_member = (
        None
        if logs_text is None
        else (WORKSPACE_LOGS_MEMBER_NAME, logs_text, time.time())
    )
    encoded, _ = encode_zip_capped(logs_member, chat_members)
    payload: dict[str, object] = {
        "contract_version": CONTRACT_VERSION,
        "omissions": omissions,
    }
    if encoded is not None:
        payload["zip"] = encoded
    print(json.dumps(payload))


if __name__ == "__main__":
    main(sys.argv[1:])

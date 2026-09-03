#!/usr/bin/env python3
"""Resident in-workspace collector for bug-report diagnostics.

Invoked by the minds desktop app via a small ``mngr exec`` as
``python3 system/scripts/collect_bug_report_diagnostics.py [--logs]
[--transcript] [--scan-timeout=<seconds>]``. Stdlib only (no venv, no
third-party imports), targeting the container's system python3 (3.11+).

Prints exactly one line -- the base64 of a zip -- and nothing at all when no
content type was requested. The zip holds ``metadata.json`` plus one
``logs/<program>.log`` member per collected log (when --logs scanned clean)
and one ``chats/<agent-name>-<harness>.jsonl`` per selected agent
conversation, newest first (when --transcript scanned clean). Anything
requested that was withheld is a plain-words line in the archive's own
``collection-notes.txt`` member, so the archive explains itself; a content
type that was not requested appears in neither the members nor the notes.

Nothing leaves the container unscanned: every chat, the logs text, and each
future zip member's own filename are staged as PLAINTEXT and run through the
template's own secret-scan gate before anything is packed. A scanner pointed
at compressed bytes matches nothing and reports clean, which would turn the
archive into a way around the scan, so the scan always happens first.
"""

import base64
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
from collections.abc import Sequence
from datetime import datetime

WORKSPACE_DIR = "/home/user/workspace"
# The workspace's own service definitions, read for the supervisorctl status in
# the report's metadata.
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
# conversation.
TRANSCRIPT_RECENCY_WINDOW_SECONDS = 2 * 60 * 60

# The floor on how many conversations ride along: the newest
# MIN_TRANSCRIPT_COUNT chats attach even when the recency window holds fewer,
# because a bug filed from a quiet workspace still needs its recent history --
# and a stale workspace is exactly where the conversation is hardest to
# reconstruct from anything else. A workspace with fewer chats sends them all.
MIN_TRANSCRIPT_COUNT = 5

# How far back a service log still describes the workspace the bug was filed
# from: a log nothing has written to in over a day is history, not diagnostics,
# and only pads the archive.
LOG_RECENCY_WINDOW_SECONDS = 24 * 60 * 60

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

# Plain-words notes for the ``collection-notes.txt`` member. The archive is
# the only channel back to the report, so anything withheld says so here.
NOTES_MEMBER_NAME = "collection-notes.txt"
NOTE_SCANNER_UNAVAILABLE = "withheld: the secret scanner could not run, so nothing it was to check was released"
NOTE_SECRETS_FOUND = "withheld: the secret scan reported findings"
NOTE_NO_CHAT_TRANSCRIPT = "no chat transcripts exist in this workspace"

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


def program_name_for_log(path: str) -> str:
    """The program a log file belongs to, from its filename.

    The bare ``.log`` case covers files without a stream suffix (supervisord's
    own log); a program whose stderr file and plain log share a stem is kept
    apart by the member-name numbering, not here.
    """
    name = os.path.basename(path)
    for suffix in ("-stderr.log", "-stdout.log", ".log"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def select_log_files() -> list[str]:
    """Every supervisord log file worth sending, newest first and capped.

    Deliberately unfiltered by owner or stream: any program's log -- app or
    service, user-created or built-in, stdout or stderr -- can carry the bug,
    so all of them ride (the user consented via the logs checkbox, and every
    member still passes the secret scan before it leaves).

    Only logs written to inside ``LOG_RECENCY_WINDOW_SECONDS`` are sent: a
    program that has been silent for over a day describes some earlier state of
    the workspace, not the one the bug was filed from.
    """
    candidates = list(glob.glob(SUPERVISOR_LOG_DIR + "/*.log"))
    cutoff = time.time() - LOG_RECENCY_WINDOW_SECONDS
    candidates = [p for p in candidates if safe_mtime(p) >= cutoff]
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
            "log_lines_per_file": MAX_LINES_PER_LOG,
        },
    }


def collect_log_members() -> list[tuple[str, str, float]]:
    """One member per log file, as ``(member name, content, mtime)``.

    Separate members rather than one concatenated file: the payload is an
    archive, so there is no reason to make a reader split headed sections apart
    again.
    """
    members = []
    used_names: set[str] = set()
    for path in select_log_files():
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


def list_agents(timeout: float) -> list[tuple[str, str]]:
    """Every agent, as ``(name, pinned address)`` -- chat, worker, or the services agent.

    Deliberately unfiltered by kind: any agent's conversation can carry the bug,
    so all of them are asked for a transcript (an agent with none simply
    contributes no member, which is how the primary services agent usually
    drops out).

    Scoped to the local provider on purpose: every agent inside a workspace is
    the inner mngr's own, and asking the cloud providers baked into the
    settings only makes mngr probe backends that cannot answer from inside a
    container -- that probing, not the listing, is what used to cost the
    collection most of its budget. The returned ``name@host.provider`` address
    pins each later ``mngr event`` the same way, so it skips the fan-out too.

    The pipe-delimited template is used rather than ``--format json``: inside a
    workspace container mngr cannot reach the providers that back its hosts, and
    the json path fails outright on that where the template still answers from
    local state.
    """
    listed = run_mngr(
        [
            "list",
            "--provider",
            "local",
            "--format",
            "{name}|{name}@{host.name}.{host.provider_name}",
        ],
        timeout,
    )
    if listed is None:
        return []
    agents: list[tuple[str, str]] = []
    for line in listed.splitlines():
        parts = line.split("|")
        if len(parts) != 2:
            continue
        name, address = (p.strip() for p in parts)
        if not name or not address:
            continue
        agents.append((name, address))
    return agents


def fetch_transcript(address: str, timeout: float) -> str | None:
    """One agent's conversation as raw JSONL, or None when it has none.

    ``address`` is the pinned ``name@host.provider`` form from ``list_agents``,
    so resolving it never fans out to the unreachable cloud providers.

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
            address,
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
    """The conversations to attach, as ``(member name, content, last-written epoch)``.

    Every agent's transcript is a candidate -- chat, background worker, or
    otherwise. A bug is rarely about exactly one conversation, so every one
    written to inside the recency window rides along, newest first -- and never
    fewer than the ``MIN_TRANSCRIPT_COUNT`` newest (all of them, when the
    workspace holds fewer), so a report filed from a quiet workspace still
    carries its recent history rather than nothing.
    """
    fetched = []
    used_names: set[str] = set()
    for name, address in list_agents(timeout):
        events = fetch_transcript(address, timeout)
        if events is None:
            continue
        member = transcript_member_name(name, transcript_source(events), used_names)
        fetched.append((member, events, newest_event_time(events)))
    if not fetched:
        return []
    fetched.sort(key=lambda item: item[2], reverse=True)
    cutoff = time.time() - TRANSCRIPT_RECENCY_WINDOW_SECONDS
    # Sorted newest first, so the in-window chats are a prefix: one slice keeps
    # every recent chat and tops up to the floor from the newest of the rest.
    recent_count = sum(1 for item in fetched if item[2] >= cutoff)
    return fetched[: max(recent_count, MIN_TRANSCRIPT_COUNT)]

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


def encode_zip(members: Sequence[tuple[str, str, float]]) -> str | None:
    """Everything collected, packed and base64-encoded; None when there is nothing.

    Deliberately unbounded. The payload returns on the collector's stdout, which
    carries it whole -- 32MB was measured arriving intact -- so there is no
    transport cliff to stay under, and S3 does not care either. What a large
    payload costs is time (roughly 0.45s per MB on top of a ~4s floor) against
    the host's collection budget, and a collection that outgrows that budget
    fails loudly as ``exec_timeout`` rather than quietly shipping a trimmed set
    a reader could not tell from a complete one.
    """
    if not members:
        return None
    return base64.b64encode(build_zip(list(members))).decode("ascii")


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
        return {path: NOTE_SCANNER_UNAVAILABLE for path in target_paths}
    if proc.returncode == 0:
        return {path: None for path in target_paths}
    # scan_secrets.sh reports on stderr, but stdout is folded in so that a
    # failure printed anywhere still counts against the scan.
    output = proc.stdout + "\n" + proc.stderr
    if any(marker in output for marker in SCANNER_MALFUNCTION_MARKERS):
        return {path: NOTE_SCANNER_UNAVAILABLE for path in target_paths}
    if UNPARSEABLE_REPORT_MARKER in output:
        return {path: NOTE_SECRETS_FOUND for path in target_paths}
    finding_lines = [line for line in output.splitlines() if FINDING_MARKER in line]
    if not finding_lines:
        return {path: NOTE_SCANNER_UNAVAILABLE for path in target_paths}
    verdicts = {}
    for path in target_paths:
        matched = [line for line in finding_lines if path in line]
        verdicts[path] = NOTE_SECRETS_FOUND if matched else None
    unattributed = [
        line for line in finding_lines if not any(path in line for path in target_paths)
    ]
    if unattributed:
        return {path: NOTE_SECRETS_FOUND for path in target_paths}
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
    # Plain-words lines for the notes member: whenever something requested is
    # not in the archive, one line here says so. The archive is the only
    # channel back to the report.
    notes: list[str] = []

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
            notes.append("recent chats: " + NOTE_NO_CHAT_TRANSCRIPT)

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
                notes.append("workspace logs: " + NOTE_SCANNER_UNAVAILABLE)
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
                notes.append("recent chats: " + NOTE_SCANNER_UNAVAILABLE)
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
                    verdicts.get(path, NOTE_SCANNER_UNAVAILABLE) for path in paths
                ]
                reason = next((r for r in reasons if r is not None), None)
                if reason is not None:
                    if key == WORKSPACE_LOGS_KEY:
                        log_members = []
                        notes.append("workspace logs: " + reason)
                    if key == TRANSCRIPT_KEY:
                        chat_members = []
                        notes.append("recent chats: " + reason)
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)

    # The notes ride inside the archive itself, so a reader learns what was
    # withheld from the same file that holds what was not. Collector-authored
    # text only -- no workspace content -- so it is not itself scanned.
    members = list(log_members) + list(chat_members)
    if notes:
        members.append((NOTES_MEMBER_NAME, "\n".join(notes) + "\n", time.time()))
    # Base64 because stdout is text; the host decodes the one line and stages
    # the archive verbatim. Nothing prints when nothing was requested.
    encoded = encode_zip(members)
    if encoded is not None:
        print(encoded)


if __name__ == "__main__":
    main(sys.argv[1:])

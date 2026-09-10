#!/usr/bin/env python3
"""Resident in-workspace collector for bug-report diagnostics.

Invoked by the minds desktop app via a small ``mngr exec`` as
``python3 system/scripts/collect_bug_report_diagnostics.py [--logs]
[--transcript] [--scan-timeout=<seconds>]``. Stdlib only (no venv, no
third-party imports), targeting the container's system python3 (3.11+).

Prints exactly one line -- the base64 of a zip -- and nothing at all when no
content type was requested. Under --logs the zip holds ``metadata.json`` plus
one ``logs/<program>.log`` member per collected service log, and one
``agent-logs/<agent-name>/<file>`` per harness log -- services run under
supervisord and agents run under tmux, so the two halves come from different
places and neither sees the other's failures. A harness that keeps a structured
log database rather than a text log (codex's app-server) contributes it too,
rendered to text as ``agent-logs/<agent-name>/<db>.log``. Under --transcript it
holds one ``chats/<agent-name>-<harness>.jsonl`` per selected agent
conversation, newest first. With BOTH flags each running agent also contributes
an ``agent-logs/<agent-name>/pane.txt``: a TUI harness renders the conversation
into its pane, so its scrollback needs the chats consent as well as the logs
one. The workspace logs, the harness logs, the log databases and the chats are
each scanned and released or withheld on their own. Anything requested that is
not in the archive whole -- a class withheld by the secret scan, or one the size
budget could not fit -- is a plain-words line in the archive's own
``collection-notes.txt`` member, so the archive explains itself; a content type
that was not requested appears in neither the members nor the notes.

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
import sqlite3
import subprocess
import sys
import tempfile
import time
import zipfile
from collections.abc import Sequence
from datetime import datetime, timezone
from functools import lru_cache

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
AGENT_LOGS_KEY = "agent_logs"
AGENT_LOG_DB_KEY = "agent_log_db"

MAX_LOG_FILES = 100
# Generous because MAX_READ_BYTES, not this, is what bounds a member: the tail
# is already read and then thrown away above this line. A chatty service can
# write 200 lines in under a second, which used to make a report filed minutes
# after the bug carry none of it.
MAX_LINES_PER_LOG = 2000
MAX_SHED_LINES = 50
# /proc/meminfo is ~50 lines; this keeps every headline figure and drops the
# hugepage tail.
MAX_MEMINFO_LINES = 40
# Ceiling on how much of any one file is read. supervisord rotates each log at
# 10MB, so reading 100 of them whole would cost a gigabyte for 200 lines each.
MAX_READ_BYTES = 256 * 1024
# Ceiling on the text one log class contributes, filled newest-first. The
# per-file cap alone does not bound a class: a hundred files at MAX_READ_BYTES
# each is 25MB of plaintext, all of which the secret scanner would have to read
# inside the host's scan budget before anything could be released.
MAX_LOG_CLASS_BYTES = 4 * 1024 * 1024
# Well under the host's collection budget, so a slow scan degrades to
# scanner_unavailable instead of consuming the whole budget. The host overrides
# it via --scan-timeout to keep it the same fraction of whatever budget it runs
# under.
DEFAULT_SCAN_TIMEOUT_SECONDS = 12
SUPERVISORCTL_TIMEOUT_SECONDS = 2
DF_TIMEOUT_SECONDS = 2
GIT_TIMEOUT_SECONDS = 2
# How long a log-db read waits on SQLite's own lock. Short because a live
# harness holds the write lock in short bursts, and a report that cannot get a
# read in seconds is better off saying so than spending the collection budget.
LOG_DB_TIMEOUT_SECONDS = 2

# Where each kind of member lives inside the archive.
METADATA_MEMBER_NAME = "metadata.json"
LOG_MEMBER_DIR = "logs"
CHAT_MEMBER_DIR = "chats"
AGENT_LOG_MEMBER_DIR = "agent-logs"

# What the agent half of a report is read out of: the per-agent state
# directories mngr keeps as siblings of this collector's own. Taken from the env
# mngr sources into the ``mngr exec`` that runs this script rather than rebuilt
# from the host dir, so the collector holds no copy of mngr's layout -- and
# empty, collecting nothing, when this is not running under an agent's env at
# all, rather than guessing a path under ``$HOME`` that may belong to somebody
# else's mngr.
AGENTS_DIR = os.path.dirname(os.environ.get("MNGR_AGENT_STATE_DIR", ""))

# The harness diagnostics under one agent's state dir, as globs rather than a
# list of harnesses: codex writes ``app_server.log`` and a
# ``plugin/codex/home/tui_log/`` tail, antigravity and the TUI harnesses write
# under ``logs/`` or a bare ``<name>.log``. ``events/`` is deliberately not
# reachable from any of these -- it holds the conversation, which the chats
# half already collects, and the converter's own stdout, which says nothing.
#
# They are also this script's one piece of mngr-layout knowledge, so a harness
# that logs anywhere else is uncollected and says nothing about being so.
AGENT_LOG_GLOBS = ("*.log", "logs/*.log", "plugin/*/home/tui_log/*.log")

# Harness log databases, read alongside the text logs above. Codex writes one:
# its app-server keeps a structured SQLite log whose rows carry the request and
# connection each line belongs to, which the text log it writes to stderr does
# not. The db is also the only place that attribution is affordable -- codex
# builds its stderr layer with ``FmtSpan::FULL``, so asking for the same spans
# there costs an ``enter``/``exit`` record per poll of every future, measured at
# 99% of that file's bytes.
#
# Matched by glob for the same reason AGENT_LOG_GLOBS is: the numeric suffix is
# a schema version codex bumps on migration, and the next one should be picked
# up without a template release.
AGENT_LOG_DB_GLOBS = ("plugin/*/home/logs_*.sqlite",)

# The table every harness log db is read through, and the columns a rendered row
# is built from. A db without this shape contributes nothing rather than a
# partial render, so a schema change reads as "no rows" and not as silence.
LOG_DB_TABLE = "logs"
# Rows per db, newest first before rendering. Deliberately larger than the
# MAX_READ_BYTES trim that follows, so the byte ceiling is what bounds the
# member and a run of short rows still fills it.
MAX_LOG_DB_ROWS = 4000

# The only rows read out of a harness log db: those its own request/response
# layer wrote. This is an allowlist rather than a denylist of the targets known
# to log credentials, because the db holds the harness's whole TRACE stream --
# including its HTTP client, which logs the Authorization header it just sent.
# Measured on a live codex workspace: bearer JWTs under two targets, and a
# separate API key under a third (``codex_core::session::turn``) that a denylist
# built from the first two did not anticipate. An allowlist can only ever miss
# something useful; a denylist misses something secret.
#
# It costs nothing the db was collected for: the request and connection each
# line belongs to is exactly what this layer records.
#
# Each entry names a module, matched exactly or at the ``::`` separator below it
# -- never as a bare character prefix, which would also admit every crate whose
# name merely starts the same way (``codex_app_server_protocol`` carries the
# serialized protocol messages, including the login exchange). Admitting a
# target nobody listed is the denylist failure this allowlist exists to avoid.
#
# The transport crate is listed separately for that reason: it is a sibling of
# ``codex_app_server``, not a child, so an exact-module match does not reach it.
# It is worth naming because it is where the socket and websocket come up, which
# is the whole record of a daemon that never started -- and those lines land at
# startup, which is exactly what a busy day pushes out of app_server.log's tail.
LOG_DB_TARGET_MODULES = ("codex_app_server", "codex_app_server_transport")

# Ceiling on the text the log-db class contributes, filled newest-first. Its own
# rather than shared with the harness log files, because the two are separate
# classes -- see the note on AGENT_LOG_DB_KEY in main().
MAX_LOG_DB_CLASS_BYTES = 1024 * 1024

# The scrollback of an agent's primary tmux window, captured for harnesses whose
# crash output only ever reaches the screen.
#
# It needs BOTH consents, which is why it is not simply part of the logs class:
# a TUI harness renders the conversation into its pane, so the scrollback is
# chat content as much as it is diagnostics. A user who asks for logs and
# declines to send their chats has declined this too.
PANE_MEMBER_NAME = "pane.txt"
MAX_PANE_LINES = 1000

# Panes are budgeted apart from the harness log files rather than competing with
# them. They are captured now, so they would sort newest under any shared
# newest-first budget and displace the log files of the agent that actually
# broke; and being the only record for a harness that writes no file, they must
# not be squeezed out by a busy one either.
MAX_PANE_CLASS_BYTES = 512 * 1024

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
NOTE_NO_AGENT_LOGS = "no agent wrote a harness log to collect"
# A class the byte budget could not fit whole. Said out loud because otherwise a
# trimmed class reads exactly like a complete one, and "the harness that broke
# wrote nothing" is the wrong conclusion to leave a reader holding.
NOTE_TRIMMED_TO_BUDGET = "{} file(s) were left out to fit the collection's size budget"

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


def format_log_db_row(
    ts: int, ts_nanos: int, level: str, target: str, body: str
) -> str:
    """One log-db row in the shape the harness's own text logs already use.

    Timestamp, level, target, message, in that order, so a reader moving between
    the rendered db and ``app_server.log`` does not have to learn a second
    layout. ``ts_nanos`` is the sub-second part, kept in full because the rows
    that matter for a stuck turn are the ones microseconds apart.
    """
    moment = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return "{}.{:09d}Z {:>5} {}: {}".format(
        moment, ts_nanos, level, target, body.strip()
    )


def read_log_db(path: str) -> str:
    """The newest allowlisted rows of a harness log db, rendered oldest-first, or a note.

    Opened read-only through a URI so a live harness keeps writing undisturbed;
    a bug report observes the workspace, it does not pause it. Read-only also
    reads the write-ahead log, which is where a busy harness's most recent --
    and most relevant -- rows still are: an ``immutable=1`` open, the other way
    to read a db without writing, silently skips them.

    Only LOG_DB_TARGET_MODULES rows are selected, in SQL rather than after, so
    the rest are never read into the collector at all.

    Rendered oldest-first after being selected newest-first, so the member reads
    forward like every other log in the archive while the trim still drops the
    oldest rows rather than the newest.
    """
    try:
        connection = sqlite3.connect(
            "file:{}?mode=ro".format(path), uri=True, timeout=LOG_DB_TIMEOUT_SECONDS
        )
    except sqlite3.Error as e:
        return "(unreadable: {!r})".format(e)
    # GLOB rather than LIKE: LIKE reads ``_`` as a single-character wildcard, which
    # would quietly widen an allowlist whose every module name contains one. The
    # module itself is matched by equality and its children at the ``::`` below it,
    # so a crate that merely starts with the same characters is not a child.
    target_clause = " OR ".join(
        "target = ? OR target GLOB ?" for _ in LOG_DB_TARGET_MODULES
    )
    target_parameters: list[str] = []
    for module in LOG_DB_TARGET_MODULES:
        target_parameters.extend((module, "{}::*".format(module)))
    try:
        rows = connection.execute(
            "SELECT ts, ts_nanos, level, target, feedback_log_body FROM {}"
            " WHERE {} ORDER BY id DESC LIMIT ?".format(LOG_DB_TABLE, target_clause),
            (*target_parameters, MAX_LOG_DB_ROWS),
        ).fetchall()
    except sqlite3.Error as e:
        return "(unreadable: {!r})".format(e)
    finally:
        connection.close()

    # Filled newest-first up to the byte ceiling and then reversed, so the trim
    # drops the oldest rows -- the same end-is-the-news rule read_tail applies to
    # the text logs. Whole rows only: a byte-offset trim would leave the first
    # line a fragment starting mid-timestamp, and unlike a text log this one is
    # being rendered here, so it can be cut where a reader would cut it.
    newest_first: list[str] = []
    budget = MAX_READ_BYTES
    for ts, ts_nanos, level, target, body in rows:
        # SQLite stores what it was given, not what the column was declared as, so a
        # bumped schema can hand back a NULL level or a ts that is not a number --
        # neither of which is a sqlite3.Error. Unguarded, that ends the whole
        # collection and the report arrives with nothing in it at all.
        #
        # OSError belongs in this list because a ts outside the platform's time_t
        # is what ``datetime.fromtimestamp`` raises it for, and a ts moved to
        # nanoseconds is exactly that -- the plainest form the anticipated schema
        # bump could take. Nothing here touches the filesystem (the rows are
        # already fetched), so there is no file-level OSError for it to swallow.
        try:
            line = format_log_db_row(ts, ts_nanos, level, target, body or "")
        except (TypeError, ValueError, OverflowError, OSError) as e:
            return "(unreadable: {!r})".format(e)
        cost = len(line.encode("utf-8")) + 1
        if cost > budget:
            break
        budget -= cost
        newest_first.append(line)
    return "\n".join(reversed(newest_first))


def select_agent_log_dbs(agent_id: str) -> list[str]:
    """One agent's harness log databases, newest first.

    Unlike the text logs these carry no recency filter: a db is written through
    for the life of the harness rather than rotated, so its mtime describes the
    last write and not the span it covers, and the rows carry their own times
    anyway.
    """
    if not AGENTS_DIR:
        return []
    agent_dir = os.path.join(AGENTS_DIR, agent_id)
    paths: set[str] = set()
    for pattern in AGENT_LOG_DB_GLOBS:
        paths.update(glob.glob(os.path.join(agent_dir, pattern)))
    return sorted(paths, key=safe_mtime, reverse=True)


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


def take_within_byte_budget(
    members: Sequence[tuple[str, str, float]], budget_bytes: int
) -> tuple[list[tuple[str, str, float]], int]:
    """The longest prefix of ``members`` fitting ``budget_bytes``, and how many were dropped.

    A prefix rather than a best-fit selection: callers order newest-first, so
    stopping at the budget drops the least recent members, and a reader can
    tell what is missing (older) from what is there. The first member always
    rides even when it alone overruns, so a single outsized log cannot empty
    the class.

    The dropped count is returned because the archive is the only channel back
    to the report: a trimmed class has to be able to say so, or a reader cannot
    tell it from a complete one.
    """
    kept: list[tuple[str, str, float]] = []
    spent = 0
    for index, member in enumerate(members):
        cost = len(member[1].encode("utf-8"))
        if kept and spent + cost > budget_bytes:
            return kept, len(members) - index
        kept.append(member)
        spent += cost
    return kept, 0


def collect_log_members() -> tuple[list[tuple[str, str, float]], int]:
    """One member per log file, as ``(member name, content, mtime)``, and the budget's drop count.

    Separate members rather than one concatenated file: the payload is an
    archive, so there is no reason to make a reader split headed sections apart
    again.
    """
    members = []
    used_names: set[str] = set()
    for path in select_log_files():
        stem = safe_member_component(program_name_for_log(path))
        member = unique_member_name(
            "{}/{}.log".format(LOG_MEMBER_DIR, stem), used_names
        )
        members.append((member, read_tail(path, MAX_LINES_PER_LOG), safe_mtime(path)))
    return take_within_byte_budget(members, MAX_LOG_CLASS_BYTES)


def select_agent_log_files(agent_id: str) -> list[str]:
    """One agent's harness log files, newest first, within the same recency window as the services'.

    Ordered newest-first so the class byte budget spends itself on whatever was
    written to most recently -- for a bug filed against a chat, that is the
    harness that was running when it broke.
    """
    if not AGENTS_DIR:
        return []
    agent_dir = os.path.join(AGENTS_DIR, agent_id)
    paths: set[str] = set()
    for pattern in AGENT_LOG_GLOBS:
        paths.update(glob.glob(os.path.join(agent_dir, pattern)))
    cutoff = time.time() - LOG_RECENCY_WINDOW_SECONDS
    recent = [path for path in paths if safe_mtime(path) >= cutoff]
    recent.sort(key=safe_mtime, reverse=True)
    return recent


def capture_pane(address: str, timeout: float) -> str | None:
    """One agent's tmux scrollback, or None when it could not be captured.

    ``--no-start`` for the same reason the host's ``mngr exec`` passes it: a bug
    report asks a workspace what happened, it does not boot anything to find
    out. An agent that is stopped therefore contributes no pane, which is
    correct -- its pane no longer exists.
    """
    captured = run_mngr(["capture", address, "--full", "--no-start"], timeout)
    if captured is None or not captured.strip():
        return None
    return "\n".join(captured.splitlines()[-MAX_PANE_LINES:])


def collect_agent_log_db_members(
    timeout: float,
) -> tuple[list[tuple[str, str, float]], int]:
    """Every agent's rendered harness log db, as ``(member name, content, mtime)``, and the drop count.

    Separate from the harness log files, and scanned as its own class, because
    the two fail differently. A log db is the harness's own TRACE stream and has
    already been observed carrying credentials it wrote about itself; a stderr
    log is what the harness chose to print. Sharing a class would mean one row
    the allowlist did not anticipate costs the report ``app_server.log`` and
    every agent's ``stderr.log`` too -- the logs most likely to explain the bug.
    """
    members: list[tuple[str, str, float]] = []
    used_names: set[str] = set()
    for name, _address, agent_id in list_agents(timeout):
        agent_dir_member = safe_member_component(name)
        for path in select_agent_log_dbs(agent_id):
            # Rendered to text under a ``.log`` member name: the db is packed for
            # a reader, not for a sqlite client.
            member = unique_member_name(
                "{}/{}/{}.log".format(
                    AGENT_LOG_MEMBER_DIR,
                    agent_dir_member,
                    safe_member_component(os.path.basename(path)),
                ),
                used_names,
            )
            members.append((member, read_log_db(path), safe_mtime(path)))
    members.sort(key=lambda item: item[2], reverse=True)
    return take_within_byte_budget(members, MAX_LOG_DB_CLASS_BYTES)


def collect_agent_log_members(
    timeout: float, is_pane_included: bool
) -> tuple[list[tuple[str, str, float]], int]:
    """The harness diagnostics for every agent, as ``(member name, content, mtime)``, and the drop count.

    Agents run in tmux, not under supervisord, so none of this reaches the
    service logs the other half collects: without it a report carries what a
    chat *said* and nothing about why its harness misbehaved.

    Every agent contributes, for the same reason every agent contributes a
    transcript -- the chat a user filed from is rarely the only one that
    matters. Log files are ordered newest-first before the class budget is
    applied, so a quiet agent never crowds out the busy one; panes fill their own
    budget so the two never compete (see ``MAX_PANE_CLASS_BYTES``).

    ``is_pane_included`` carries the chats consent: the pane holds the rendered
    conversation, so it rides only when the user asked for their chats too.

    The two budgets' drop counts are summed: the note they feed says the archive
    is short, and which of the two did the trimming is not something a reader
    can act on.
    """
    log_members: list[tuple[str, str, float]] = []
    pane_members: list[tuple[str, str, float]] = []
    used_names: set[str] = set()
    for name, address, agent_id in list_agents(timeout):
        agent_dir_member = safe_member_component(name)
        for path in select_agent_log_files(agent_id):
            member = unique_member_name(
                "{}/{}/{}".format(
                    AGENT_LOG_MEMBER_DIR,
                    agent_dir_member,
                    safe_member_component(os.path.basename(path)),
                ),
                used_names,
            )
            log_members.append(
                (member, read_tail(path, MAX_LINES_PER_LOG), safe_mtime(path))
            )
        if not is_pane_included:
            continue
        pane = capture_pane(address, timeout)
        if pane is not None:
            member = unique_member_name(
                "{}/{}/{}".format(
                    AGENT_LOG_MEMBER_DIR, agent_dir_member, PANE_MEMBER_NAME
                ),
                used_names,
            )
            pane_members.append((member, pane, time.time()))
    log_members.sort(key=lambda item: item[2], reverse=True)
    kept_logs, dropped_logs = take_within_byte_budget(log_members, MAX_LOG_CLASS_BYTES)
    kept_panes, dropped_panes = take_within_byte_budget(
        pane_members, MAX_PANE_CLASS_BYTES
    )
    return kept_logs + kept_panes, dropped_logs + dropped_panes


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


def unique_member_name(member: str, used_names: set[str]) -> str:
    """``member``, or the next numbered variant of it that no member holds yet.

    Two agents whose names sanitize alike, or one agent whose harness writes the
    same basename under two directories, would otherwise pack a member twice and
    the second would clobber the first on extraction.
    """
    stem, extension = os.path.splitext(member)
    candidate = member
    index = 2
    while candidate in used_names:
        candidate = "{}-{}{}".format(stem, index, extension)
        index += 1
    used_names.add(candidate)
    return candidate


def transcript_member_name(name: str, harness: str, used_names: set[str]) -> str:
    """The zip member name for one chat, unique within the archive.

    Carries the agent's name and the harness that wrote it, under the fixed ``chats/``
    directory, so the conversations stay tellable apart with nothing injected
    into the transcript itself. The member keeps the harness's own .jsonl, so it
    opens in whatever reads a transcript normally.
    """
    stem = "{}-{}".format(safe_member_component(name), safe_member_component(harness))
    return unique_member_name("{}/{}.jsonl".format(CHAT_MEMBER_DIR, stem), used_names)


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


@lru_cache(maxsize=1)
def _query_agents(timeout: float) -> tuple[tuple[str, str, str], ...]:
    """Ask mngr which agents exist, at most once per run.

    Cached because it is the collector's most expensive call (see the fan-out
    note below) and each half of a report needs the same answer. ``timeout``
    here is most of the whole collection's budget, so asking three times is how
    a slow mngr turns a trimmed report into no report at all.

    A failed listing is cached too: retrying a wedged mngr is exactly what the
    budget cannot afford. Returned as a tuple, so what is cached cannot be
    mutated through a caller's copy.

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
            "{name}|{name}@{host.name}.{host.provider_name}|{id}",
        ],
        timeout,
    )
    if listed is None:
        return ()
    agents: list[tuple[str, str, str]] = []
    for line in listed.splitlines():
        parts = line.split("|")
        if len(parts) != 3:
            continue
        name, address, agent_id = (p.strip() for p in parts)
        if not name or not address or not agent_id:
            continue
        agents.append((name, address, agent_id))
    return tuple(agents)


def list_agents(timeout: float) -> list[tuple[str, str, str]]:
    """Every agent, as ``(name, pinned address, id)`` -- chat, worker, or the services agent.

    The id is what names an agent's state directory, so it is asked for here
    rather than derived: mngr owns the mapping from an agent to its own files.

    One listing serves the whole report, so the harness logs, the log databases
    and the chats all describe the same set of agents.
    """
    return list(_query_agents(timeout))


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
    for name, address, _agent_id in list_agents(timeout):
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


def stage_members(
    staging_dir: str, key: str, members: Sequence[tuple[str, str, float]]
) -> list[str] | None:
    """One class's members staged as plaintext, or None when one could not be written.

    Every staged file leads with the zip member name it will be packed under:
    the name is written into the archive's directory in plaintext, it is built
    from a directory name inside the workspace, and the sanitizer that shapes it
    keeps every character a credential is written with.
    """
    staged: list[str] = []
    for index, (name, content, _) in enumerate(members):
        path = stage_for_scan(
            staging_dir, "{}-{}".format(key, index), name + "\n" + content
        )
        if path is None:
            return None
        staged.append(path)
    return staged


def main(argv: Sequence[str]) -> None:
    flags = set(argv)
    scan_timeout_seconds = parse_scan_timeout(list(flags))
    # Plain-words lines for the notes member: whenever something requested is
    # not in the archive, one line here says so. The archive is the only
    # channel back to the report.
    notes: list[str] = []

    # The classes the archive is built from, each released or withheld on its
    # own so one chat carrying a secret costs the report its conversations and
    # not its logs. The label is what a note calls the class in plain words;
    # members ride the archive in this order.
    collected: list[tuple[str, str, list[tuple[str, str, float]]]] = []
    if "--logs" in flags:
        log_members, dropped_logs = collect_log_members()
        if dropped_logs:
            notes.append(
                "workspace logs: " + NOTE_TRIMMED_TO_BUDGET.format(dropped_logs)
            )
        collected.append(
            (
                WORKSPACE_LOGS_KEY,
                "workspace logs",
                [
                    (
                        METADATA_MEMBER_NAME,
                        json.dumps(build_metadata(), indent=2),
                        time.time(),
                    ),
                    *log_members,
                ],
            )
        )
        # The pane needs the chats consent as well as this one: it is where a TUI
        # harness renders the conversation, so its scrollback is chat content and
        # not only diagnostics.
        agent_log_members, dropped_agent_logs = collect_agent_log_members(
            scan_timeout_seconds, is_pane_included="--transcript" in flags
        )
        if not agent_log_members:
            notes.append("agent logs: " + NOTE_NO_AGENT_LOGS)
        elif dropped_agent_logs:
            notes.append(
                "agent logs: " + NOTE_TRIMMED_TO_BUDGET.format(dropped_agent_logs)
            )
        collected.append((AGENT_LOGS_KEY, "agent logs", agent_log_members))

        # Its own class, not part of the agent logs above: a harness log db is
        # the harness's whole TRACE stream and has been observed carrying
        # credentials the harness logged about itself, so a row the target
        # allowlist did not anticipate must cost the report this class alone.
        log_db_members, dropped_log_dbs = collect_agent_log_db_members(
            scan_timeout_seconds
        )
        if dropped_log_dbs:
            notes.append(
                "agent log databases: " + NOTE_TRIMMED_TO_BUDGET.format(dropped_log_dbs)
            )
        collected.append((AGENT_LOG_DB_KEY, "agent log databases", log_db_members))

    if "--transcript" in flags:
        chat_members = collect_transcript_members(scan_timeout_seconds)
        if not chat_members:
            notes.append("recent chats: " + NOTE_NO_CHAT_TRANSCRIPT)
        collected.append((TRANSCRIPT_KEY, "recent chats", chat_members))

    # Nothing leaves the container unscanned, so a payload that cannot even be
    # staged for the scanner is dropped exactly as one the scanner could not
    # read. Each member stages one plaintext file, because the scanner has to
    # read the content itself -- it cannot see inside the archive it is packed
    # into afterwards.
    staging_dir = tempfile.mkdtemp(prefix="bug-report-scan-")
    withheld: dict[str, str] = {}
    try:
        staged_by_key: dict[str, list[str]] = {}
        for key, _label, class_members in collected:
            if not class_members:
                continue
            staged = stage_members(staging_dir, key, class_members)
            if staged is None:
                withheld[key] = NOTE_SCANNER_UNAVAILABLE
            else:
                staged_by_key[key] = staged

        if staged_by_key:
            targets = [path for paths in staged_by_key.values() for path in paths]
            verdicts = scan_targets(targets, scan_timeout_seconds)
            for key, paths in staged_by_key.items():
                # A class made of several files is released only when every one
                # of them is clean: one chat carrying a secret withholds the
                # whole class rather than quietly shipping a partial set of
                # conversations, which a reader could not tell from the full set.
                reasons = [
                    verdicts.get(path, NOTE_SCANNER_UNAVAILABLE) for path in paths
                ]
                reason = next((r for r in reasons if r is not None), None)
                if reason is not None:
                    withheld[key] = reason
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)

    members: list[tuple[str, str, float]] = []
    for key, label, class_members in collected:
        if key in withheld:
            notes.append("{}: {}".format(label, withheld[key]))
            continue
        members.extend(class_members)

    # The notes ride inside the archive itself, so a reader learns what was
    # withheld from the same file that holds what was not. Collector-authored
    # text only -- no workspace content -- so it is not itself scanned.
    if notes:
        members.append((NOTES_MEMBER_NAME, "\n".join(notes) + "\n", time.time()))
    # Base64 because stdout is text; the host decodes the one line and stages
    # the archive verbatim. Nothing prints when nothing was requested.
    encoded = encode_zip(members)
    if encoded is not None:
        print(encoded)


if __name__ == "__main__":
    main(sys.argv[1:])

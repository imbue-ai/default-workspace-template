#!/usr/bin/env python3
"""Import the user's Claude and ChatGPT chats into this workspace with datalib.

Subcommands, run from the repo root (every skill's cwd)::

    python3 .agents/skills/import-chats/scripts/import_chats.py check --source claude
    python3 .agents/skills/import-chats/scripts/import_chats.py sync --source claude --source chatgpt
    python3 .agents/skills/import-chats/scripts/import_chats.py status

``check`` asks the source's own API, through ``latchkey curl``, whether this workspace can read
the account yet, and prints one JSON line ``{"source", "state", "detail"}``. Its ``state`` says
what the agent does next: ``connected`` (sync), ``needs_permission`` (file the source's
permission request; a re-sent request also signs a lapsed account back in), or ``blocked`` (the
site refused the request itself, which a sign-in does not fix).

``sync`` installs datalib through the datalib skill's installer (without the Datalib app), adds
each named source to the datalib store's config if it is not there yet, and runs one
``datalib-dag`` sync of the named sources' downloads. It records each source's progress in the
status file as it goes, prints the final status, and exits 0 only when every named source imported.
A sync of a large account takes a while: run it through ``system/scripts/run_in_background.py``.

``status`` prints the status file.

The store is the datalib skill's (``data/.skills/datalib/``), shared with anything else the user
mirrors there. A source this script adds gets an ingest step (the download into the raw store
``<group>/ingest/entities.doltlite_db``), a ``render_markdown`` step and a ``keyword_index`` step,
and the last two are turned off before the config holding them is put in place: a first import
only downloads, which is what keeps it quick and light. Turning them on, which the skill does only
when the user asks for search over their chats, is ``datalib-dag turn-on``. A source already in
the config is never rewritten, so whatever was turned on or changed there stays.

The status file (``data/.skills/import-chats/status.json``) is read by the Getting Started app's
"Bring in your chats" card, so its shape is a contract with
``system/apps/getting_started/src/getting_started/chat_import.py``::

    {"sources": {"claude": {"state": "imported", "conversations": 312,
                            "updated_at": "<iso8601>", "detail": "", "pid": null,
                            "fetched": null, "to_fetch": null}}}

``state`` is one of ``importing``, ``imported``, ``needs_sign_in`` and ``failed``; ``pid`` is the
sync's process while it is ``importing``, and a running sync rewrites its record (``updated_at``)
every ``PROGRESS_INTERVAL_SECONDS``, so a reader can tell a sync that died (its pid gone, or its
record gone stale) from one that is still running. ``conversations`` is how many conversations
the raw store holds. ``fetched`` of ``to_fetch`` is how far a running ingest has got through the
conversations it set out to fetch; both are null when it reports no total, and once the sync ends.

After each sync it also rewrites one index per source beside the status file
(``data/.skills/import-chats/claude-chats.md``, ``chatgpt-chats.md``): every conversation by title,
newest first, each linked to the original and, once search is on, to its rendered page.

Standard library only, so it runs under bare ``python3`` like the other skill scripts.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import fcntl
import json
import os
import re
import subprocess
import sys
import threading
import tomllib
import urllib.parse
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

DATA_ROOT = Path("data") / ".skills" / "datalib"
STATUS_PATH = Path("data") / ".skills" / "import-chats" / "status.json"
INSTALL_SCRIPT = (
    Path(".agents") / "skills" / "datalib" / "scripts" / "install_datalib.sh"
)
SYNC_REQUESTER = "import-chats"

STATE_IMPORTING = "importing"
STATE_IMPORTED = "imported"
STATE_NEEDS_SIGN_IN = "needs_sign_in"
STATE_FAILED = "failed"

CHECK_CONNECTED = "connected"
CHECK_NEEDS_PERMISSION = "needs_permission"
CHECK_BLOCKED = "blocked"

# Added with each source, and turned off at once: they render a page per conversation and build its keyword
# index, which is most of a first sync's time and memory.
SEARCH_FUNCTIONS = ("render_markdown", "keyword_index")

# Marks a request for the gateway's Chrome-impersonating curl, which is what gets it past the
# Cloudflare challenge both sites put in front of their APIs. datalib sends the same marker.
_IMPERSONATE_HEADER = "X-Imbue-Impersonate: 1"
_CHECK_TIMEOUT_SECONDS = 60.0
# The first install downloads the whole release and its runtime.
_INSTALL_TIMEOUT_SECONDS = 900.0
_DATALIB_COMMAND_TIMEOUT_SECONDS = 120.0
# Short enough that a slow count never stalls the heartbeat past the Getting Started app's staleness limit.
_COUNT_TIMEOUT_SECONDS = 20.0
# Reading the index's rows walks every downloaded conversation, and is not on the heartbeat.
_INDEX_READ_TIMEOUT_SECONDS = _DATALIB_COMMAND_TIMEOUT_SECONDS
# How often a running sync rewrites each source's record, so the Getting Started card counts up as chats arrive.
PROGRESS_INTERVAL_SECONDS = 10.0

# latchkey's own refusals: no permission for the service yet, or no saved sign-in for it.
_LATCHKEY_REFUSALS = (
    "No service matches URL",
    "No credentials found",
    "Request not permitted",
)
# latchkey's refusals, and the provider answers datalib classifies as a credential problem: each
# is fixed by (re-)sending the source's permission request, which signs the user in again.
_SIGN_IN_MARKERS = (
    *_LATCHKEY_REFUSALS,
    "HTTP 401",
    "HTTP 403",
    "token_expired",
)
# The run summary's ``failure`` kinds this script treats specially: a credential that failed, and a download that
# gave up with work still owed after keeping what it got (the next sync resumes it).
_AUTH_FAILURE_KIND = "auth"
_INCOMPLETE_FAILURE_KIND = "incomplete"
# The statuses datalib counts as a step finishing without failing: a sync skips a step with nothing new to do.
_STEP_OK_STATUSES = ("succeeded", "skipped_up_to_date")


@dataclass(frozen=True)
class ChatSource:
    key: str
    label: str
    datalib_type: str
    group: str
    # A cheap authenticated read: it answers 200 once the account is connected.
    check_url: str
    # The raw store's conversations as ``id, title, updated_at`` rows; a row whose detail has not been fetched yet
    # has no payload.
    conversations_query: str
    conversation_url: str
    projects_query: str | None = None
    project_url: str | None = None


SOURCES: dict[str, ChatSource] = {
    "claude": ChatSource(
        key="claude",
        label="Claude",
        datalib_type="claude",
        group="claude_chats",
        check_url="https://claude.ai/api/organizations",
        conversations_query="SELECT id, name AS title, updated_at FROM conversations WHERE payload IS NOT NULL",
        conversation_url="https://claude.ai/chat/{id}",
        projects_query="SELECT id, name AS title, updated_at FROM projects WHERE payload IS NOT NULL",
        project_url="https://claude.ai/project/{id}",
    ),
    "chatgpt": ChatSource(
        key="chatgpt",
        label="ChatGPT",
        datalib_type="chatgpt",
        group="chatgpt_chats",
        check_url="https://chatgpt.com/backend-api/me",
        conversations_query="SELECT id, title, update_time AS updated_at FROM conversations WHERE payload IS NOT NULL",
        conversation_url="https://chatgpt.com/c/{id}",
    ),
}


_FRONTMATTER_FIELD = re.compile(r"^(\w+): (.*)$")
_INDEX_TITLE_LIMIT = 120
# What a title must escape to stay one link's text: a backslash would escape the character after it, a bracket
# would end the text, and ``<`` would start raw HTML.
_LINK_TEXT_SPECIAL = re.compile(r"([\\\[\]<])")


class ImportChatsError(Exception):
    """Something the import cannot go on past; the message is for the agent to read."""


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def read_status(path: Path) -> dict:
    """The status document, ``{"sources": {}}`` when there is none yet."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"sources": {}}
    except OSError as e:
        raise ImportChatsError(f"cannot read the status file {path}: {e}") from e
    except ValueError as e:
        raise ImportChatsError(f"the status file {path} is not JSON: {e}") from e
    if not isinstance(document, dict) or not isinstance(document.get("sources"), dict):
        raise ImportChatsError(f"the status file {path} is not a status document")
    return document


def _write_text_atomic(path: Path, text: str) -> None:
    """Write through a same-directory temp file and a rename, so a reader never sees a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        temp_path.write_text(text, encoding="utf-8")
        os.replace(temp_path, path)
    except OSError:
        with contextlib.suppress(OSError):
            temp_path.unlink(missing_ok=True)
        raise


def write_status(path: Path, document: Mapping) -> None:
    _write_text_atomic(path, json.dumps(document, indent=2) + "\n")


@contextlib.contextmanager
def _file_lock(lock_path: Path) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        yield


def status_lock(path: Path) -> contextlib.AbstractContextManager[None]:
    """Hold the status file's lock, so two syncs running at once never write back a record the other replaced."""
    return _file_lock(path.with_name(".status.lock"))


def config_lock(status_path: Path) -> contextlib.AbstractContextManager[None]:
    """Hold the lock on adding sources, so two syncs never both append the same source to the config."""
    return _file_lock(status_path.with_name(".config.lock"))


def recorded_conversations(path: Path, key: str) -> int:
    return int(read_status(path)["sources"].get(key, {}).get("conversations", 0))


def record_source(
    path: Path,
    key: str,
    state: str,
    conversations: int,
    detail: str,
    pid: int | None,
    fetch_progress: FetchProgress | None = None,
) -> None:
    try:
        with status_lock(path):
            document = read_status(path)
            document["sources"][key] = {
                "state": state,
                "conversations": conversations,
                "updated_at": _now(),
                "detail": detail,
                "pid": pid,
                "fetched": None if fetch_progress is None else fetch_progress.fetched,
                "to_fetch": None if fetch_progress is None else fetch_progress.total,
            }
            write_status(path, document)
    except OSError as e:
        raise ImportChatsError(f"cannot record the {key} import in {path}: {e}") from e


@dataclass(frozen=True)
class Datalib:
    """The datalib binaries the datalib skill's installer links into ``bin_dir``, and the data root they work on."""

    bin_dir: Path
    data_root: Path

    def binary(self, name: str) -> Path:
        return self.bin_dir / name

    @property
    def config_path(self) -> Path:
        return self.data_root / "config.toml"

    def raw_store(self, source: ChatSource) -> Path:
        return self.data_root / source.group / "ingest" / "entities.doltlite_db"


def install_datalib(
    install_script: Path, run: Callable[..., subprocess.CompletedProcess]
) -> None:
    """Install datalib, or check that it is in place, with the datalib skill's installer, leaving out the Datalib
    app's program: the import keeps datalib out of the user's launcher."""
    try:
        result = run(
            ["bash", str(install_script), "--without-app"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_INSTALL_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as e:
        raise ImportChatsError(
            f"datalib did not install within {e.timeout:.0f}s"
        ) from e
    except OSError as e:
        raise ImportChatsError(f"cannot run the datalib installer: {e}") from e
    if result.returncode != 0:
        said = (result.stderr.strip() or result.stdout.strip()).splitlines()[-5:]
        raise ImportChatsError(
            "datalib could not be installed: "
            + (" / ".join(said) or f"the installer exited {result.returncode}")
        )


def _run_datalib(
    datalib: Datalib,
    run: Callable[..., subprocess.CompletedProcess],
    name: str,
    *arguments: str,
) -> subprocess.CompletedProcess:
    try:
        return run(
            [str(datalib.binary(name)), *arguments],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_DATALIB_COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as e:
        raise ImportChatsError(
            f"{name} {' '.join(arguments)} did not answer within {e.timeout:.0f}s"
        ) from e
    except OSError as e:
        raise ImportChatsError(f"cannot run {name}: {e}") from e


def source_stanza(source: ChatSource) -> str:
    """The config entries for one source: its group, its download, and its two search steps."""
    group = source.group
    return "\n".join(
        [
            f"# {source.label} conversations, added by the chat import (.agents/skills/import-chats), which turned",
            "# its render_markdown and keyword_index steps off when it added them: until they are turned on, a sync",
            "# only downloads.",
            "[[groups]]",
            f'id = "{group}"',
            f'name = "{source.label}"',
            f'type = "{source.datalib_type}"',
            "",
            "[[steps]]",
            f'group = "{group}"',
            'function = "ingest"',
            "[steps.params.api]",
            "",
            "[[steps]]",
            f'group = "{group}"',
            'function = "render_markdown"',
            f'inputs = ["{group}/ingest"]',
            "",
            "[[steps]]",
            f'group = "{group}"',
            'function = "keyword_index"',
            f'inputs = ["{group}/render_markdown"]',
            "",
        ]
    )


def _configured_groups(config_text: str, config_path: Path) -> dict[str, str]:
    """Each group the config names, by id, with its type."""
    try:
        document = tomllib.loads(config_text)
    except tomllib.TOMLDecodeError as e:
        raise ImportChatsError(
            f"{config_path} is not valid TOML ({e}); fix it before importing"
        ) from e
    groups = document.get("groups", [])
    return {
        str(group["id"]): str(group.get("type", ""))
        for group in groups
        if isinstance(group, dict) and "id" in group
    }


def ensure_sources(
    datalib: Datalib,
    named: Sequence[ChatSource],
    run: Callable[..., subprocess.CompletedProcess],
) -> list[ChatSource]:
    """Add each of ``named`` that the config lacks, with its search steps turned off, and return the ones added.

    The rest of the config, other sources' and anything already there for these, is left as it is. The new config
    replaces the old one (a loop running on this root re-reads it) only once ``datalib-dag --check`` accepts it and
    the new search steps are off, so no add, failed or cut short, leaves a source that would render on its sync."""
    config_path = datalib.config_path
    try:
        previous = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        previous = None
    groups = _configured_groups(previous or "", config_path)
    for source in named:
        configured_type = groups.get(source.group)
        if configured_type is not None and configured_type != source.datalib_type:
            raise ImportChatsError(
                f"{config_path} already has a group {source.group!r} of type {configured_type!r}, not "
                f"{source.datalib_type!r}; the import will not change it"
            )
    added = [source for source in named if source.group not in groups]
    if not added:
        return []
    stanzas = "\n".join(source_stanza(source) for source in added)
    existing = (previous or "").rstrip("\n")
    _put_config_with_search_off(
        datalib, f"{existing}\n\n{stanzas}" if existing else stanzas, added, run
    )
    return added


def _put_config_with_search_off(
    datalib: Datalib,
    text: str,
    added: Sequence[ChatSource],
    run: Callable[..., subprocess.CompletedProcess],
) -> None:
    """Replace the config with ``text`` once ``datalib-dag --check`` accepts it and ``added``'s search steps are
    turned off; a refused one is never put in place.

    The steps are turned off against the candidate file, which sits in the data root beside the config (datalib
    takes the root from the file's directory), so the config in place never holds a source whose search is on."""
    config_path = datalib.config_path
    candidate = config_path.with_name(f"{config_path.name}.import-chats")
    candidate.parent.mkdir(parents=True, exist_ok=True)
    try:
        candidate.write_text(text, encoding="utf-8")
        checked = _run_datalib(datalib, run, "datalib-dag", "--check", str(candidate))
        if checked.returncode != 0:
            said = (checked.stdout + checked.stderr).strip()
            raise ImportChatsError(
                f"datalib refused the config with the chat sources added:\n{said}"
            )
        _turn_off_search(datalib, candidate, added, run)
        os.replace(candidate, config_path)
    finally:
        candidate.unlink(missing_ok=True)


def _turn_off_search(
    datalib: Datalib,
    config_path: Path,
    added: Sequence[ChatSource],
    run: Callable[..., subprocess.CompletedProcess],
) -> None:
    """Turn off each of ``added``'s search steps, named in ``config_path``, raising at the first one datalib
    refuses."""
    for source in added:
        for function in SEARCH_FUNCTIONS:
            step = f"{source.group}/{function}"
            turned_off = _run_datalib(
                datalib,
                run,
                "datalib-dag",
                "turn-off",
                str(config_path),
                step,
                "--by",
                SYNC_REQUESTER,
            )
            if turned_off.returncode != 0:
                said = turned_off.stderr.strip() or turned_off.stdout.strip()
                raise ImportChatsError(f"could not turn off {step}: {said}")


def query_raw_store(
    datalib: Datalib,
    source: ChatSource,
    sql: str,
    run: Callable[..., subprocess.CompletedProcess],
    timeout_seconds: float,
) -> list[dict] | None:
    """The rows ``sql`` reads from the source's raw store within ``timeout_seconds``; None when there is no store
    yet. A store that cannot be read raises ``ImportChatsError`` saying why."""
    store = datalib.raw_store(source)
    doltlite = datalib.binary("datalib-doltlite")
    # datalib-doltlite creates a database it is pointed at that does not exist.
    if not store.is_file() or not doltlite.is_file():
        return None
    try:
        result = run(
            [str(doltlite), "-readonly", "-json", str(store), sql],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as e:
        raise ImportChatsError(
            f"reading {store} did not finish within {e.timeout:.0f}s"
        ) from e
    except OSError as e:
        raise ImportChatsError(f"cannot run datalib-doltlite: {e}") from e
    if result.returncode != 0:
        said = (
            result.stderr.strip()
            or result.stdout.strip()
            or f"datalib-doltlite exited {result.returncode}"
        )
        raise ImportChatsError(f"cannot read {store}: {said}")
    # A query that matches nothing prints nothing.
    if not result.stdout.strip():
        return []
    try:
        rows = json.loads(result.stdout)
    except ValueError as e:
        raise ImportChatsError(
            f"datalib-doltlite's answer from {store} is not JSON: {e}"
        ) from e
    if not isinstance(rows, list):
        raise ImportChatsError(
            f"datalib-doltlite's answer from {store} is not a list of rows"
        )
    return [row for row in rows if isinstance(row, dict)]


def count_conversations(
    datalib: Datalib,
    source: ChatSource,
    run: Callable[..., subprocess.CompletedProcess],
) -> int | None:
    """How many conversations the source's raw store holds; None when there is no store yet."""
    rows = query_raw_store(
        datalib,
        source,
        f"SELECT count(*) AS n FROM ({source.conversations_query})",
        run,
        _COUNT_TIMEOUT_SECONDS,
    )
    if rows is None:
        return None
    if not rows or not isinstance(rows[0].get("n"), int):
        raise ImportChatsError(
            f"the {source.label} raw store did not answer its count: {rows!r}"
        )
    return rows[0]["n"]


@dataclass(frozen=True)
class IndexEntry:
    title: str
    original_url: str
    updated_at: datetime.datetime | None
    page: Path | None
    is_project: bool


def parse_updated_at(raw: object) -> datetime.datetime | None:
    """A raw store's timestamp: ISO 8601 text (Claude), or a JSON-encoded epoch in seconds (ChatGPT)."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        decoded = json.loads(raw)
    except ValueError:
        decoded = raw
    try:
        if isinstance(decoded, (int, float)) and not isinstance(decoded, bool):
            return datetime.datetime.fromtimestamp(decoded, tz=datetime.timezone.utc)
        if isinstance(decoded, str):
            parsed = datetime.datetime.fromisoformat(decoded)
            return (
                parsed
                if parsed.tzinfo is not None
                else parsed.replace(tzinfo=datetime.timezone.utc)
            )
    except (ValueError, OverflowError, OSError):
        return None
    return None


def _frontmatter_value(raw: str) -> str:
    """A frontmatter value as datalib writes it: a JSON string (its ``yaml_scalar``), so quotes, backslashes and line
    breaks come back as they were. A value that is not one is taken as written, without its outer quotes."""
    raw = raw.strip()
    if raw.startswith('"'):
        try:
            return str(json.loads(raw))
        except ValueError:
            return raw.strip('"')
    return raw


def rendered_pages_by_id(datalib: Datalib, source: ChatSource) -> dict[str, Path]:
    """Each page the source's render step wrote, by the ``external_id`` in its frontmatter (the conversation's or
    project's id); empty while search is off."""
    rendered = datalib.data_root / source.group / "render_markdown"
    pages: dict[str, Path] = {}
    if not rendered.is_dir():
        return pages
    for path in rendered.rglob("all.md"):
        with path.open(encoding="utf-8", errors="replace") as page:
            if page.readline() != "---\n":
                continue
            for line in page:
                if line == "---\n":
                    break
                match = _FRONTMATTER_FIELD.match(line.rstrip("\n"))
                if match is not None and match.group(1) == "external_id":
                    pages[_frontmatter_value(match.group(2))] = path
                    break
    return pages


def read_index_entries(
    datalib: Datalib,
    source: ChatSource,
    run: Callable[..., subprocess.CompletedProcess],
) -> list[IndexEntry] | None:
    """Every conversation (and project) the source's raw store holds; None when there is no store yet."""
    pages = rendered_pages_by_id(datalib, source)
    queries = [(source.conversations_query, source.conversation_url, False)]
    if source.projects_query is not None and source.project_url is not None:
        queries.append((source.projects_query, source.project_url, True))
    entries: list[IndexEntry] = []
    for sql, url, is_project in queries:
        rows = query_raw_store(datalib, source, sql, run, _INDEX_READ_TIMEOUT_SECONDS)
        if rows is None:
            return None
        for row in rows:
            row_id = str(row.get("id", ""))
            entries.append(
                IndexEntry(
                    title=str(row.get("title") or "").strip() or "(untitled)",
                    original_url=url.format(id=urllib.parse.quote(row_id)),
                    updated_at=parse_updated_at(row.get("updated_at")),
                    page=pages.get(row_id),
                    is_project=is_project,
                )
            )
    return entries


def _index_line(entry: IndexEntry, index_dir: Path) -> str:
    # A chat's name can hold tabs and runs of spaces; collapsed, the entry reads as one line.
    title = " ".join(entry.title.split())
    if len(title) > _INDEX_TITLE_LIMIT:
        title = title[: _INDEX_TITLE_LIMIT - 1].rstrip() + "…"
    title = _LINK_TEXT_SPECIAL.sub(r"\\\1", title)
    line = f"[{title}]({entry.original_url})"
    if entry.updated_at is not None:
        line = f"{entry.updated_at.date().isoformat()} · {line}"
    if entry.page is not None:
        target = urllib.parse.quote(
            Path(os.path.relpath(entry.page, index_dir)).as_posix()
        )
        line += f" · [page]({target})"
    return f"- {line}"


def render_index(
    source: ChatSource, entries: Sequence[IndexEntry], index_dir: Path
) -> str:
    """The source's index: its conversations by month, newest first, then any without a date, then its projects."""
    chats = sorted(
        (
            entry
            for entry in entries
            if not entry.is_project and entry.updated_at is not None
        ),
        key=lambda entry: (entry.updated_at, entry.title),
        reverse=True,
    )
    undated = sorted(
        (
            entry
            for entry in entries
            if not entry.is_project and entry.updated_at is None
        ),
        key=lambda entry: entry.title,
    )
    projects = sorted(
        (entry for entry in entries if entry.is_project), key=lambda entry: entry.title
    )
    conversation_count = len(chats) + len(undated)
    lines = [
        f"# {source.label} chats",
        "",
        f"{conversation_count:,} conversation{'' if conversation_count == 1 else 's'}, most recently updated first. "
        f'Each title opens it in {source.label}; "page" opens the copy in this workspace, once search is on.',
    ]
    month = None
    for entry in chats:
        assert entry.updated_at is not None
        entry_month = entry.updated_at.strftime("%B %Y")
        if entry_month != month:
            month = entry_month
            lines += ["", f"## {month}", ""]
        lines.append(_index_line(entry, index_dir))
    for heading, group in (("Undated", undated), ("Projects", projects)):
        if group:
            lines += ["", f"## {heading}", ""]
            lines += [_index_line(entry, index_dir) for entry in group]
    return "\n".join(lines) + "\n"


def index_path_for(status_path: Path, source: ChatSource) -> Path:
    return status_path.parent / f"{source.key}-chats.md"


def write_index(
    datalib: Datalib,
    source: ChatSource,
    index_path: Path,
    run: Callable[..., subprocess.CompletedProcess],
) -> None:
    """Rewrite the source's index from its raw store; a store that is missing or cannot be read raises and leaves the
    index as it was."""
    entries = read_index_entries(datalib, source, run)
    if entries is None:
        raise ImportChatsError(
            f"there is no {source.label} raw store at {datalib.raw_store(source)} yet"
        )
    _write_text_atomic(index_path, render_index(source, entries, index_path.parent))


def _event(line: str) -> dict | None:
    """The NDJSON event on ``line`` of ``datalib-dag``'s stderr, or None for any other line."""
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        event = json.loads(line)
    except ValueError:
        return None
    return event if isinstance(event, dict) and "event" in event else None


def parse_run_summary(stderr: str) -> dict[str, dict]:
    """Each step's entry in the ``run_summary`` event ``datalib-dag`` writes last to stderr (its event stream), by
    step id. A step that is turned off has no entry."""
    for line in reversed(stderr.splitlines()):
        event = _event(line)
        if event is not None and event.get("event") == "run_summary":
            return {
                step["step"]: step
                for step in event.get("steps", [])
                if isinstance(step, dict) and "step" in step
            }
    return {}


def runner_error(result: subprocess.CompletedProcess) -> str:
    """What a ``datalib-dag`` run that never summarised said about why: its stderr without the event stream (the
    runner's own error), else its stdout report, else its exit code."""
    said = "\n".join(
        line for line in result.stderr.splitlines() if _event(line) is None
    )
    return (
        said.strip()
        or result.stdout.strip()
        or f"datalib-dag exited {result.returncode}"
    )


def classify_failure(error: str, failure_kind: str | None) -> str:
    """A failed source's state: a sign-in problem the user can fix by signing in again, or any other failure.

    ``failure_kind`` is the run summary's ``failure`` for the step, which datalib sets to ``auth`` when the
    credential is what failed; the markers catch the refusals it does not classify that way."""
    if failure_kind == _AUTH_FAILURE_KIND or any(
        marker in error for marker in _SIGN_IN_MARKERS
    ):
        return STATE_NEEDS_SIGN_IN
    return STATE_FAILED


def failure_detail(error: str) -> str:
    """The line of a step's error that says what went wrong (datalib's ``caused by:`` line when there is one)."""
    lines = [line.strip() for line in error.splitlines() if line.strip()]
    for line in lines:
        if line.startswith("caused by:"):
            return line.removeprefix("caused by:").strip()
    return lines[-1] if lines else "the import failed"


def source_outcome(
    steps: Mapping[str, dict], source: ChatSource, result: subprocess.CompletedProcess
) -> tuple[str, str]:
    """``source``'s state after a finished ``datalib-dag`` run, and the detail that goes with it.

    The download is what decides it: a source whose ingest committed is imported, with a detail when the ingest
    gave up partway (what it got is kept, and the next sync resumes) or when its search steps, if they are on,
    failed. A run with no summary at all handed its request to a loop another process was already running on the
    same data root, and exits with that request's outcome: 0 when it was done."""
    if not steps:
        if result.returncode == 0:
            return STATE_IMPORTED, ""
        error = runner_error(result)
        return classify_failure(error, None), failure_detail(error)
    ingest = steps.get(f"{source.group}/ingest")
    if ingest is None:
        return STATE_FAILED, failure_detail(runner_error(result))
    error = str(ingest.get("error", ""))
    if ingest.get("status") not in _STEP_OK_STATUSES:
        if ingest.get("failure") == _INCOMPLETE_FAILURE_KIND:
            return (
                STATE_IMPORTED,
                f"not every conversation came in ({failure_detail(error)}); an update fetches the rest",
            )
        return classify_failure(error, ingest.get("failure")), failure_detail(error)
    for function in SEARCH_FUNCTIONS:
        step = steps.get(f"{source.group}/{function}")
        if step is not None and step.get("status") not in _STEP_OK_STATUSES:
            return (
                STATE_IMPORTED,
                f"search could not be updated: {failure_detail(str(step.get('error', '')))}",
            )
    return STATE_IMPORTED, ""


@dataclass(frozen=True)
class FetchProgress:
    """How far a source's ingest has got through the conversations it set out to fetch this run."""

    fetched: int
    total: int


class IngestProgress:
    """Each ingest step's progress, read off ``datalib-dag``'s event stream as it arrives.

    An ingest that knows how much it has to fetch says so with ``progress_length`` and counts each item with
    ``progress_inc`` (datalib's ChatGPT ingest does; its Claude ingest reports no length, so it has no entry here).
    A ``step_start`` begins the count again: ``datalib-dag`` retries a failed ingest from zero, with a new length.
    Fed from the thread reading the stream and read by the one recording progress, hence the lock.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._total_by_step: dict[str, int] = {}
        self._fetched_by_step: dict[str, int] = {}

    def observe(self, line: str) -> None:
        event = _event(line)
        if event is None or not str(event.get("step", "")).endswith("/ingest"):
            return
        step = str(event["step"])
        with self._lock:
            match event.get("event"):
                case "step_start":
                    self._total_by_step.pop(step, None)
                    self._fetched_by_step.pop(step, None)
                case "progress_length" if isinstance(event.get("total"), int):
                    self._total_by_step[step] = event["total"]
                case "progress_inc" if isinstance(event.get("delta"), int):
                    self._fetched_by_step[step] = (
                        self._fetched_by_step.get(step, 0) + event["delta"]
                    )

    def of(self, source: ChatSource) -> FetchProgress | None:
        step = f"{source.group}/ingest"
        with self._lock:
            total = self._total_by_step.get(step)
            if total is None:
                return None
            return FetchProgress(
                fetched=min(self._fetched_by_step.get(step, 0), total), total=total
            )


def _record_progress_until(
    is_done: threading.Event,
    named: Sequence[ChatSource],
    datalib: Datalib,
    status_path: Path,
    progress: IngestProgress,
    run: Callable[..., subprocess.CompletedProcess],
    interval_seconds: float,
) -> None:
    while not is_done.wait(interval_seconds):
        for source in named:
            try:
                record_source(
                    status_path,
                    source.key,
                    STATE_IMPORTING,
                    _current_conversations(datalib, source, status_path, run),
                    "",
                    pid=os.getpid(),
                    fetch_progress=progress.of(source),
                )
            except ImportChatsError as e:
                print(
                    f"import-chats: could not record the {source.label} import's progress: {e}",
                    file=sys.stderr,
                )


def stream_process(
    command: Sequence[str], on_stderr_line: Callable[[str], None]
) -> subprocess.CompletedProcess:
    """Run ``command``, handing each line of its stderr to ``on_stderr_line`` as it arrives.

    ``datalib-dag`` writes its event stream to stderr, so this is how a sync is followed while it runs. stdout is
    drained on its own thread so neither pipe can fill and stall the process. Returns everything both streams
    said, as ``subprocess.run`` would, and like it kills the command when following it fails.
    """
    with subprocess.Popen(
        list(command),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    ) as process:
        stdout, stderr = process.stdout, process.stderr
        assert stdout is not None and stderr is not None
        stdout_chunks: list[str] = []
        stdout_reader = threading.Thread(
            target=lambda: stdout_chunks.append(stdout.read()),
            name="datalib-dag-stdout",
            daemon=True,
        )
        stdout_reader.start()
        stderr_lines: list[str] = []
        try:
            for line in stderr:
                stderr_lines.append(line)
                on_stderr_line(line)
        except BaseException:
            process.kill()
            raise
        finally:
            stdout_reader.join()
        returncode = process.wait()
    return subprocess.CompletedProcess(
        list(command), returncode, "".join(stdout_chunks), "".join(stderr_lines)
    )


@contextlib.contextmanager
def _reporting_progress(
    named: Sequence[ChatSource],
    datalib: Datalib,
    status_path: Path,
    progress: IngestProgress,
    run: Callable[..., subprocess.CompletedProcess],
    interval_seconds: float,
) -> Iterator[None]:
    """Record each source's progress every ``interval_seconds`` for as long as the block runs.

    Each record also refreshes ``updated_at``, which the Getting Started app reads as the sync's heartbeat: a
    record that says ``importing`` but has not been rewritten for a while belongs to a sync that died, whatever
    its pid says now (pids start over when the container restarts). A record that cannot be written is reported on
    stderr and tried again at the next tick, so one failed write does not stop the heartbeat."""
    is_done = threading.Event()
    reporter = threading.Thread(
        target=_record_progress_until,
        args=(is_done, named, datalib, status_path, progress, run, interval_seconds),
        name="import-chats-progress",
        daemon=True,
    )
    reporter.start()
    try:
        yield
    finally:
        is_done.set()
        reporter.join()


def _run_dag(
    named: Sequence[ChatSource],
    datalib: Datalib,
    stream: Callable[
        [Sequence[str], Callable[[str], None]], subprocess.CompletedProcess
    ],
    progress: IngestProgress,
) -> subprocess.CompletedProcess:
    """Run one ``datalib-dag`` sync of ``named``'s ingests, feeding its event stream to ``progress``. A sync runs
    what is downstream of them too, which is nothing while their search steps are off."""
    roots = ",".join(f"{source.group}/ingest" for source in named)
    return stream(
        [
            str(datalib.binary("datalib-dag")),
            str(datalib.config_path),
            "--sync",
            roots,
            "--by",
            SYNC_REQUESTER,
        ],
        progress.observe,
    )


def _current_conversations(
    datalib: Datalib,
    source: ChatSource,
    status_path: Path,
    run: Callable[..., subprocess.CompletedProcess],
) -> int:
    """How many conversations the source's raw store holds, else (no store yet, or one that cannot be read, which is
    reported on stderr) the count last recorded."""
    try:
        counted = count_conversations(datalib, source, run)
    except ImportChatsError as e:
        print(
            f"import-chats: the {source.label} conversations could not be counted, so the last count stands: {e}",
            file=sys.stderr,
        )
        counted = None
    return (
        counted
        if counted is not None
        else recorded_conversations(status_path, source.key)
    )


def _record_failed(
    named: Sequence[ChatSource],
    datalib: Datalib,
    status_path: Path,
    detail: str,
    run: Callable[..., subprocess.CompletedProcess],
) -> None:
    for source in named:
        record_source(
            status_path,
            source.key,
            STATE_FAILED,
            _current_conversations(datalib, source, status_path, run),
            detail,
            pid=None,
        )


def sync(
    named: Sequence[ChatSource],
    datalib: Datalib,
    install_script: Path,
    status_path: Path,
    run: Callable[..., subprocess.CompletedProcess],
    stream: Callable[
        [Sequence[str], Callable[[str], None]], subprocess.CompletedProcess
    ] = stream_process,
    progress_interval_seconds: float = PROGRESS_INTERVAL_SECONDS,
) -> bool:
    """Import ``named``: install datalib if needed, add any source the config lacks, run one sync, and record each
    source's outcome. True when every named source imported.

    From the install on, a thread records how many conversations each raw store holds, and how far each ingest has
    got, every ``progress_interval_seconds`` until the sync returns."""
    for source in named:
        record_source(
            status_path,
            source.key,
            STATE_IMPORTING,
            recorded_conversations(status_path, source.key),
            "",
            pid=os.getpid(),
        )
    progress = IngestProgress()
    try:
        with _reporting_progress(
            named, datalib, status_path, progress, run, progress_interval_seconds
        ):
            install_datalib(install_script, run)
            with config_lock(status_path):
                ensure_sources(datalib, named, run)
            result = _run_dag(named, datalib, stream, progress)
    except ImportChatsError as e:
        _record_failed(named, datalib, status_path, str(e), run)
        raise
    except OSError as e:
        _record_failed(named, datalib, status_path, str(e), run)
        raise ImportChatsError(str(e)) from e
    return record_outcomes(named, result, datalib, status_path, run)


def record_outcomes(
    named: Sequence[ChatSource],
    result: subprocess.CompletedProcess,
    datalib: Datalib,
    status_path: Path,
    run: Callable[..., subprocess.CompletedProcess],
) -> bool:
    """Record each named source's outcome from a finished ``datalib-dag`` run and rewrite its index. True when every
    named source imported; an index that cannot be rewritten is reported on stderr and leaves the outcome as it is.
    A source with no raw store yet (its first import failed before downloading anything) has no index to write."""
    steps = parse_run_summary(result.stderr)
    is_every_source_imported = True
    for source in named:
        state, detail = source_outcome(steps, source, result)
        is_every_source_imported = is_every_source_imported and state == STATE_IMPORTED
        record_source(
            status_path,
            source.key,
            state,
            _current_conversations(datalib, source, status_path, run),
            detail,
            pid=None,
        )
    for source in named:
        if not datalib.raw_store(source).is_file():
            continue
        index_path = index_path_for(status_path, source)
        try:
            write_index(datalib, source, index_path, run)
        except (OSError, ImportChatsError) as e:
            print(
                f"import-chats: the {source.label} chats are recorded, but their index {index_path} could not be "
                f"rewritten: {e}",
                file=sys.stderr,
            )
    return is_every_source_imported


def classify_check(returncode: int, stdout: str, stderr: str) -> tuple[str, str]:
    """The ``check`` state for a ``latchkey curl -w '\\n%{http_code}'`` result: ``(state, detail)``.

    The status code is the last line of stdout; latchkey's refusals may come on either stream."""
    body, _, status_code = stdout.rstrip("\n").rpartition("\n")
    if returncode == 0 and status_code.strip() == "200":
        return CHECK_CONNECTED, ""
    for marker in _LATCHKEY_REFUSALS:
        if marker in stdout or marker in stderr:
            return CHECK_NEEDS_PERMISSION, marker
    code = status_code.strip()
    if code == "401":
        return CHECK_NEEDS_PERMISSION, "the saved sign-in has expired"
    if code == "403" and "cloudflare" not in body.lower():
        return CHECK_NEEDS_PERMISSION, "the account refused the saved sign-in"
    if code == "403":
        return CHECK_BLOCKED, "the site's bot protection refused the request"
    return CHECK_BLOCKED, (
        body.strip() or stderr.strip() or f"latchkey curl exited {returncode}"
    )[:300]


def check(
    source: ChatSource, run: Callable[..., subprocess.CompletedProcess]
) -> tuple[str, str]:
    command = [
        "latchkey",
        "curl",
        "-sS",
        "--max-time",
        str(int(_CHECK_TIMEOUT_SECONDS)),
        "-H",
        _IMPERSONATE_HEADER,
        "-w",
        "\n%{http_code}",
        source.check_url,
    ]
    try:
        # The body is the site's own answer, so a byte that is not UTF-8 must not stop the check.
        result = run(command, capture_output=True, encoding="utf-8", errors="replace")
    except OSError as e:
        raise ImportChatsError(f"cannot run latchkey: {e}") from e
    return classify_check(result.returncode, result.stdout, result.stderr)


def _sources(keys: Sequence[str]) -> list[ChatSource]:
    return [SOURCES[key] for key in dict.fromkeys(keys)]


def main(
    argv: Sequence[str] | None = None,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n", 1)[0])
    subcommands = parser.add_subparsers(dest="command", required=True)
    check_parser = subcommands.add_parser(
        "check", help="whether this workspace can read the account yet"
    )
    check_parser.add_argument("--source", required=True, choices=sorted(SOURCES))
    sync_parser = subcommands.add_parser("sync", help="import the named sources")
    sync_parser.add_argument(
        "--source", required=True, action="append", choices=sorted(SOURCES)
    )
    subcommands.add_parser("status", help="print what has been imported so far")
    arguments = parser.parse_args(argv)

    try:
        if arguments.command == "check":
            source = SOURCES[arguments.source]
            state, detail = check(source, run)
            print(json.dumps({"source": source.key, "state": state, "detail": detail}))
            return 0
        if arguments.command == "sync":
            # The datalib skill's installer links the binaries here (DATALIB_BIN_DIR moves them, as it does there).
            bin_dir = Path(
                os.environ.get("DATALIB_BIN_DIR", Path.home() / ".local" / "bin")
            )
            is_imported = sync(
                _sources(arguments.source),
                Datalib(bin_dir=bin_dir, data_root=DATA_ROOT),
                INSTALL_SCRIPT,
                STATUS_PATH,
                run,
            )
            print(json.dumps(read_status(STATUS_PATH), indent=2))
            return 0 if is_imported else 1
        print(json.dumps(read_status(STATUS_PATH), indent=2))
        return 0
    except ImportChatsError as e:
        print(f"import-chats: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

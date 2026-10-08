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

``sync`` installs datalib on first use (the pinned release, sha256-checked, plus the Node runtime
its latchkey and search steps run on), writes datalib's config for every source this workspace has
ever imported, and runs one ``datalib-dag`` sync of the named sources. It records each source's
progress in the status file as it goes, prints the final status, and exits 0 only when every named
source imported. A sync of a large account takes a while: run it through
``system/scripts/run_in_background.py``.

``status`` prints the status file.

The status file (``data/.skills/import-chats/status.json``) is read by the Getting Started app's
"Bring in your chats" card, so its shape is a contract with
``system/apps/getting_started/src/getting_started/chat_import.py``::

    {"sources": {"claude": {"state": "imported", "conversations": 312,
                            "updated_at": "<iso8601>", "detail": "", "pid": null,
                            "fetched": null, "to_fetch": null}}}

``state`` is one of ``importing``, ``imported``, ``needs_sign_in`` and ``failed``; ``pid`` is the
sync's process while it is ``importing``, so a reader can tell a sync that died from one that is
still running. ``fetched`` of ``to_fetch`` is how far a running ingest has got through the
conversations it set out to fetch; both are null when it reports no total, and once the sync ends.

After each sync it also rewrites one index per source beside the status file
(``data/.skills/import-chats/claude-chats.md``, ``chatgpt-chats.md``): every page by title, newest
first, each linked to its page in the workspace and to the original. datalib names the pages by
uuid, so this is the one place to browse them by name.

The datalib store is ``data/.skills/datalib/`` (its ``config.toml`` and every tree datalib writes):
the raw records each ingest kept, and one rendered markdown page per conversation under
``<group>/render_markdown/``. Credentials never touch it; latchkey's gateway attaches them.

Standard library only, so it runs under bare ``python3`` like the other skill scripts.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

DATALIB_VERSION = "v0.40.0"
# The fully static builds: they run on any Linux of the right arch, whatever its libc.
_TARBALL_SHA256_BY_ARCH = {
    "x86_64": "b777dc02be0f6ea1d1e712dfe020be6fecd5468478380a208793706f2c722b04",
    "aarch64": "488d9fa8fd232e6a5a2041762fe421c9173e1af1d4cf914e118dbcb99752b707",
}
_RELEASE_URL = "https://github.com/imbue-ai/datalib/releases/download"
_DOWNLOAD_TIMEOUT_SECONDS = 300.0
# ``pull-runtime`` downloads the Node runtime with no deadline of its own.
_PULL_RUNTIME_TIMEOUT_SECONDS = 600.0

DATA_ROOT = Path("data") / ".skills" / "datalib"
STATUS_PATH = Path("data") / ".skills" / "import-chats" / "status.json"
SYNC_REQUESTER = "import-chats"

STATE_IMPORTING = "importing"
STATE_IMPORTED = "imported"
STATE_NEEDS_SIGN_IN = "needs_sign_in"
STATE_FAILED = "failed"

CHECK_CONNECTED = "connected"
CHECK_NEEDS_PERMISSION = "needs_permission"
CHECK_BLOCKED = "blocked"

# Marks a request for the gateway's Chrome-impersonating curl, which is what gets it past the
# Cloudflare challenge both sites put in front of their APIs. datalib sends the same marker.
_IMPERSONATE_HEADER = "X-Imbue-Impersonate: 1"
_CHECK_TIMEOUT_SECONDS = 60.0
# How often a running sync rewrites each source's count, so the Getting Started card counts up as pages arrive.
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
# The run summary's ``failure`` for a step whose credential failed.
_AUTH_FAILURE_KIND = "auth"
# A render step's statuses that leave the source's pages current: a sync skips a render with nothing new to do.
_RENDER_DONE_STATUSES = ("succeeded", "skipped_up_to_date")


@dataclass(frozen=True)
class ChatSource:
    key: str
    label: str
    datalib_type: str
    group: str
    # A cheap authenticated read: it answers 200 once the account is connected.
    check_url: str


SOURCES: dict[str, ChatSource] = {
    "claude": ChatSource(
        key="claude",
        label="Claude",
        datalib_type="claude",
        group="claude_chats",
        check_url="https://claude.ai/api/organizations",
    ),
    "chatgpt": ChatSource(
        key="chatgpt",
        label="ChatGPT",
        datalib_type="chatgpt",
        group="chatgpt_chats",
        check_url="https://chatgpt.com/backend-api/me",
    ),
}


# What the index reads off each rendered page: its frontmatter, the header's link to the original, and every
# message's timestamp (the frontmatter carries no dates).
_FRONTMATTER_FIELD = re.compile(r"^(\w+): (.*)$")
_SOURCE_LINK = re.compile(r'class="source-link" href="([^"]+)"')
_MESSAGE_TIME = re.compile(r'<time class="msg-ts" datetime="([^"]+)"')
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
    except ValueError as e:
        raise ImportChatsError(f"the status file {path} is not JSON: {e}") from e
    if not isinstance(document, dict) or not isinstance(document.get("sources"), dict):
        raise ImportChatsError(f"the status file {path} is not a status document")
    return document


def _write_text_atomic(path: Path, text: str) -> None:
    """Write through a same-directory temp file and a rename, so a reader never sees a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    temp_path.write_text(text, encoding="utf-8")
    os.replace(temp_path, path)


def write_status(path: Path, document: Mapping) -> None:
    _write_text_atomic(path, json.dumps(document, indent=2) + "\n")


def record_source(
    path: Path,
    key: str,
    state: str,
    conversations: int,
    detail: str,
    pid: int | None,
    fetch_progress: FetchProgress | None = None,
) -> None:
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


def render_config(sources: Sequence[ChatSource]) -> str:
    """datalib's ``config.toml`` for ``sources``: per source, its ingest, its rendered pages and its keyword
    index; then the shared grid and search over all of them, and the applet that serves them.

    Embeddings are left out: they download a model and keep a CPU busy for a long time, and keyword search plus
    the rendered pages cover what an agent needs from past chats.
    """
    if not sources:
        raise ImportChatsError("a config needs at least one source")
    lines = [
        "# Written by the import-chats skill (.agents/skills/import-chats/scripts/import_chats.py), which",
        "# rewrites it on every import: a hand edit here does not survive the next one.",
        "",
    ]
    for source in sources:
        group = source.group
        lines += [
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
    rendered = ", ".join(f'"{source.group}/render_markdown"' for source in sources)
    indexed = ", ".join(f'"{source.group}/keyword_index"' for source in sources)
    lines += [
        "[[groups]]",
        'id = "unified_index"',
        "",
        "[[steps]]",
        'group = "unified_index"',
        'function = "grid_index"',
        f"inputs = [{rendered}]",
        "",
        "[[steps]]",
        'group = "unified_index"',
        'function = "qmd_aggregator"',
        f"inputs = [{indexed}]",
        "",
        "[[applets]]",
        'group = "unified_index"',
        'id = "unified_index"',
        'command = "datalib-applet unified_index"',
        "",
    ]
    return "\n".join(lines)


def configured_sources(
    status: Mapping, named: Sequence[ChatSource]
) -> list[ChatSource]:
    """Every source this workspace has imported before plus the ones named now, in a stable order: a sync of one
    source must not drop another from the config (or from search)."""
    keys = set(status["sources"]) | {source.key for source in named}
    return [source for key, source in SOURCES.items() if key in keys]


def rendered_pages(data_root: Path, source: ChatSource) -> list[Path]:
    """Every page the source's render step wrote: one per conversation (and, for Claude, one per project)."""
    rendered = data_root / source.group / "render_markdown"
    return list(rendered.rglob("all.md")) if rendered.is_dir() else []


def count_conversations(data_root: Path, source: ChatSource) -> int:
    return len(rendered_pages(data_root, source))


@dataclass(frozen=True)
class IndexedPage:
    title: str
    path: Path
    original_url: str
    last_message_at: datetime.datetime | None

    @property
    def is_project(self) -> bool:
        return "/project/" in self.original_url


def _frontmatter_value(raw: str) -> str:
    """A frontmatter value as datalib's chat renderer writes it: in double quotes, with only its double quotes
    escaped (backslashes are written as they are). A name with a line break leaves just its first line here."""
    raw = raw.strip()
    if raw.startswith('"'):
        return raw[1:].removesuffix('"').replace('\\"', '"')
    return raw


def _parse_time(raw: str) -> datetime.datetime | None:
    try:
        parsed = datetime.datetime.fromisoformat(raw)
    except ValueError:
        return None
    return (
        parsed
        if parsed.tzinfo is not None
        else parsed.replace(tzinfo=datetime.timezone.utc)
    )


def read_page(path: Path) -> IndexedPage:
    """What the index shows for one rendered page; a page without frontmatter is listed by its directory name."""
    text = path.read_text(encoding="utf-8", errors="replace")
    fields: dict[str, str] = {}
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        for line in text[4 : end if end != -1 else 4].splitlines():
            match = _FRONTMATTER_FIELD.match(line)
            if match is not None:
                fields[match.group(1)] = _frontmatter_value(match.group(2))
    link = _SOURCE_LINK.search(text)
    times = [
        parsed
        for raw in _MESSAGE_TIME.findall(text)
        if (parsed := _parse_time(raw)) is not None
    ]
    return IndexedPage(
        title=fields.get("display") or fields.get("title") or path.parent.name,
        path=path,
        original_url=link.group(1) if link is not None else "",
        last_message_at=max(times) if times else None,
    )


def _index_line(page: IndexedPage, index_dir: Path) -> str:
    # A chat's name can hold tabs and runs of spaces; collapsed, the entry reads as one line.
    title = " ".join(page.title.split())
    if len(title) > _INDEX_TITLE_LIMIT:
        title = title[: _INDEX_TITLE_LIMIT - 1].rstrip() + "…"
    title = _LINK_TEXT_SPECIAL.sub(r"\\\1", title)
    target = urllib.parse.quote(Path(os.path.relpath(page.path, index_dir)).as_posix())
    line = f"- [{title}]({target})"
    if page.last_message_at is not None:
        line = f"- {page.last_message_at.date().isoformat()} · [{title}]({target})"
    if page.original_url:
        line += f" · [original]({page.original_url})"
    return line


def render_index(
    source: ChatSource, pages: Sequence[IndexedPage], index_dir: Path
) -> str:
    """The source's index: its conversations by month, newest first, then any without a date, then its projects."""
    chats = sorted(
        (
            page
            for page in pages
            if not page.is_project and page.last_message_at is not None
        ),
        key=lambda page: (page.last_message_at, page.title),
        reverse=True,
    )
    undated = sorted(
        (
            page
            for page in pages
            if not page.is_project and page.last_message_at is None
        ),
        key=lambda page: page.title,
    )
    projects = sorted(
        (page for page in pages if page.is_project), key=lambda page: page.title
    )
    conversation_count = len(chats) + len(undated)
    lines = [
        f"# {source.label} chats",
        "",
        f"{conversation_count:,} conversation{'' if conversation_count == 1 else 's'}, most recent first. Each title opens "
        f'the copy in this workspace; "original" opens it in {source.label}.',
    ]
    month = None
    for page in chats:
        assert page.last_message_at is not None
        page_month = page.last_message_at.strftime("%B %Y")
        if page_month != month:
            month = page_month
            lines += ["", f"## {month}", ""]
        lines.append(_index_line(page, index_dir))
    for heading, group in (("Undated", undated), ("Projects", projects)):
        if group:
            lines += ["", f"## {heading}", ""]
            lines += [_index_line(page, index_dir) for page in group]
    return "\n".join(lines) + "\n"


def write_index(data_root: Path, source: ChatSource, index_path: Path) -> None:
    """Rewrite the source's index from every page its render step wrote."""
    pages = [read_page(path) for path in rendered_pages(data_root, source)]
    _write_text_atomic(index_path, render_index(source, pages, index_path.parent))


def index_path_for(status_path: Path, source: ChatSource) -> Path:
    return status_path.parent / f"{source.key}-chats.md"


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
    step id."""
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


@dataclass(frozen=True)
class DatalibInstall:
    """Where the pinned release lives in this home directory."""

    home: Path

    @property
    def release_dir(self) -> Path:
        return self.home / ".local" / "lib" / "datalib" / DATALIB_VERSION

    def binary(self, name: str) -> Path:
        return self.release_dir / name

    def is_installed(self) -> bool:
        return os.access(self.binary("datalib-dag"), os.X_OK)


def linux_arch(system: str, machine: str) -> str:
    """The release's arch name for this machine, refusing one the pinned release has no build for."""
    arch = {
        "x86_64": "x86_64",
        "amd64": "x86_64",
        "aarch64": "aarch64",
        "arm64": "aarch64",
    }.get(machine.lower())
    if system != "Linux" or arch is None:
        raise ImportChatsError(
            f"datalib is installed here only on Linux x86_64 or arm64, not {system} {machine}"
        )
    return arch


def _download(url: str, destination: Path) -> None:
    with (
        urllib.request.urlopen(url, timeout=_DOWNLOAD_TIMEOUT_SECONDS) as response,
        destination.open("wb") as out,
    ):
        shutil.copyfileobj(response, out)


def unpack_release(archive: Path, destination: Path, triple: str) -> Path:
    """Unpack the release tarball into ``destination`` and return the one release directory it holds."""
    try:
        with tarfile.open(archive) as opened:
            opened.extractall(destination, filter="data")
    except tarfile.TarError as e:
        raise ImportChatsError(f"{archive.name} could not be unpacked: {e}") from e
    staged = [path for path in destination.glob(f"datalib-*-{triple}") if path.is_dir()]
    if len(staged) != 1:
        raise ImportChatsError(
            f"{archive.name} holds {len(staged)} datalib-*-{triple} directories, not one"
        )
    return staged[0]


def install_datalib(
    install: DatalibInstall, run: Callable[..., subprocess.CompletedProcess]
) -> None:
    """Install the pinned release into ``install.release_dir`` unless it is already there, then fetch the Node
    runtime its latchkey and search steps use (a no-op once fetched).

    The tarball is unpacked whole, because its binaries find their siblings and ``runtime.manifest`` beside their
    own resolved path. Nothing is linked onto PATH: the tarball also carries a ``latchkey`` launcher, which must not
    shadow the workspace's own.
    """
    if not install.is_installed():
        arch = linux_arch(platform.system(), platform.machine())
        triple = f"{arch}-unknown-linux-musl"
        tarball = f"datalib-{triple}.tar.gz"
        install.release_dir.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=install.release_dir.parent) as scratch:
            scratch_path = Path(scratch)
            archive = scratch_path / tarball
            _download(f"{_RELEASE_URL}/{DATALIB_VERSION}/{tarball}", archive)
            with archive.open("rb") as downloaded:
                digest = hashlib.file_digest(downloaded, "sha256").hexdigest()
            if digest != _TARBALL_SHA256_BY_ARCH[arch]:
                raise ImportChatsError(
                    f"{tarball} has sha256 {digest}, not the pinned {_TARBALL_SHA256_BY_ARCH[arch]}"
                )
            staged = unpack_release(archive, scratch_path, triple)
            shutil.rmtree(install.release_dir, ignore_errors=True)
            os.replace(staged, install.release_dir)
    try:
        result = run(
            [str(install.binary("datalib-step")), "pull-runtime"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_PULL_RUNTIME_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as e:
        raise ImportChatsError(
            f"datalib could not fetch its runtime: no answer in {e.timeout:.0f}s"
        ) from e
    if result.returncode != 0:
        raise ImportChatsError(
            f"datalib could not fetch its runtime: {(result.stderr or result.stdout).strip()}"
        )


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
    data_root: Path,
    status_path: Path,
    progress: IngestProgress,
    interval_seconds: float,
) -> None:
    while not is_done.wait(interval_seconds):
        for source in named:
            record_source(
                status_path,
                source.key,
                STATE_IMPORTING,
                count_conversations(data_root, source),
                "",
                pid=os.getpid(),
                fetch_progress=progress.of(source),
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
        assert process.stdout is not None and process.stderr is not None
        stdout_chunks: list[str] = []
        stdout_reader = threading.Thread(
            target=lambda: stdout_chunks.append(process.stdout.read()),
            name="datalib-dag-stdout",
            daemon=True,
        )
        stdout_reader.start()
        stderr_lines: list[str] = []
        try:
            for line in process.stderr:
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


def _run_dag_with_progress(
    named: Sequence[ChatSource],
    install: DatalibInstall,
    config_path: Path,
    data_root: Path,
    status_path: Path,
    stream: Callable[
        [Sequence[str], Callable[[str], None]], subprocess.CompletedProcess
    ],
    progress_interval_seconds: float,
) -> subprocess.CompletedProcess:
    """Run one ``datalib-dag`` sync of ``named``'s ingests, recording how far each has got as it goes."""
    roots = ",".join(f"{source.group}/ingest" for source in named)
    progress = IngestProgress()
    is_done = threading.Event()
    reporter = threading.Thread(
        target=_record_progress_until,
        args=(
            is_done,
            named,
            data_root,
            status_path,
            progress,
            progress_interval_seconds,
        ),
        name="import-chats-progress",
        daemon=True,
    )
    reporter.start()
    try:
        return stream(
            [
                str(install.binary("datalib-dag")),
                str(config_path),
                "--sync",
                roots,
                "--by",
                SYNC_REQUESTER,
            ],
            progress.observe,
        )
    finally:
        is_done.set()
        reporter.join()


def _record_failed(
    named: Sequence[ChatSource], data_root: Path, status_path: Path, detail: str
) -> None:
    for source in named:
        record_source(
            status_path,
            source.key,
            STATE_FAILED,
            count_conversations(data_root, source),
            detail,
            pid=None,
        )


def sync(
    named: Sequence[ChatSource],
    install: DatalibInstall,
    data_root: Path,
    status_path: Path,
    run: Callable[..., subprocess.CompletedProcess],
    stream: Callable[
        [Sequence[str], Callable[[str], None]], subprocess.CompletedProcess
    ] = stream_process,
    progress_interval_seconds: float = PROGRESS_INTERVAL_SECONDS,
) -> bool:
    """Import ``named``: install datalib if needed, write the config, run one sync, and record each source's
    outcome. True when every named source imported.

    datalib renders and indexes pages while the ingest is still fetching, so the sync's own page count grows as
    it runs; a thread records it, with how far each ingest has got, every ``progress_interval_seconds`` until the
    sync returns."""
    for source in named:
        previous = read_status(status_path)["sources"].get(source.key, {})
        record_source(
            status_path,
            source.key,
            STATE_IMPORTING,
            int(previous.get("conversations", 0)),
            "",
            pid=os.getpid(),
        )
    try:
        install_datalib(install, run)
        data_root.mkdir(parents=True, exist_ok=True)
        config_path = data_root / "config.toml"
        config_path.write_text(
            render_config(configured_sources(read_status(status_path), named)),
            encoding="utf-8",
        )
        result = _run_dag_with_progress(
            named,
            install,
            config_path,
            data_root,
            status_path,
            stream,
            progress_interval_seconds,
        )
    except ImportChatsError as e:
        _record_failed(named, data_root, status_path, str(e))
        raise
    except OSError as e:
        _record_failed(named, data_root, status_path, str(e))
        raise ImportChatsError(str(e)) from e
    return record_outcomes(named, result, data_root, status_path)


def record_outcomes(
    named: Sequence[ChatSource],
    result: subprocess.CompletedProcess,
    data_root: Path,
    status_path: Path,
) -> bool:
    """Record each named source's outcome from a finished ``datalib-dag`` run and rewrite its index. True when every
    named source imported."""
    steps = parse_run_summary(result.stderr)
    is_every_source_imported = True
    for source in named:
        conversations = count_conversations(data_root, source)
        failure = source_failure(steps, source, result)
        if failure is None:
            record_source(
                status_path, source.key, STATE_IMPORTED, conversations, "", pid=None
            )
            continue
        is_every_source_imported = False
        error, failure_kind = failure
        record_source(
            status_path,
            source.key,
            classify_failure(error, failure_kind),
            conversations,
            failure_detail(error),
            pid=None,
        )
    for source in named:
        write_index(data_root, source, index_path_for(status_path, source))
    return is_every_source_imported


def source_failure(
    steps: Mapping[str, dict],
    source: ChatSource,
    result: subprocess.CompletedProcess,
) -> tuple[str, str | None] | None:
    """Why ``source`` did not import in a finished ``datalib-dag`` run, as its error and datalib's failure kind; None
    when it imported.

    A run with no summary at all handed its request to a loop another process was already running on the same data
    root, and exits with that request's outcome: 0 when it was done."""
    if not steps:
        return None if result.returncode == 0 else (runner_error(result), None)
    ingest = steps.get(f"{source.group}/ingest")
    if ingest is None:
        return runner_error(result), None
    if ingest.get("status") != "succeeded":
        return str(ingest.get("error", "")), ingest.get("failure")
    render = steps.get(f"{source.group}/render_markdown")
    if render is not None and render.get("status") not in _RENDER_DONE_STATUSES:
        return str(render.get("error", "")), render.get("failure")
    return None


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
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
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
            is_imported = sync(
                _sources(arguments.source),
                DatalibInstall(home=Path.home()),
                DATA_ROOT,
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

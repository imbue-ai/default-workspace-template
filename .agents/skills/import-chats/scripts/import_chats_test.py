"""Tests for the import-chats skill's script: the config it writes, how it reads datalib's and latchkey's answers,
and the status it records through a sync."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tarfile
import threading
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parent / "import_chats.py"
_spec = importlib.util.spec_from_file_location("import_chats", _SCRIPT)
assert _spec is not None and _spec.loader is not None
import_chats = importlib.util.module_from_spec(_spec)
sys.modules["import_chats"] = import_chats
_spec.loader.exec_module(import_chats)

CLAUDE = import_chats.SOURCES["claude"]
CHATGPT = import_chats.SOURCES["chatgpt"]


def _run_summary(*steps: dict) -> str:
    return json.dumps({"event": "run_summary", "steps": list(steps)})


class _FakeDatalib:
    """Stands in for the installed release's binaries: ``pull-runtime`` succeeds, and a ``datalib-dag`` sync
    renders ``pages_by_group`` pages for each source it was asked to sync, then writes ``stderr`` (the event
    stream, where the real binary writes its run summary) and ``stdout`` (its per-step report). It exits 2 when a
    step failed, like the real binary, unless ``returncode`` says otherwise."""

    def __init__(
        self,
        data_root: Path,
        stderr: str,
        pages_by_group: dict[str, int],
        stdout: str = "",
        returncode: int | None = None,
    ) -> None:
        self.data_root = data_root
        self.stderr = stderr
        self.pages_by_group = pages_by_group
        self.stdout = stdout
        self.returncode = returncode
        self.commands: list[list[str]] = []

    def __call__(
        self, command: Sequence[str], **_kwargs: object
    ) -> subprocess.CompletedProcess:
        self.commands.append(list(command))
        if command[1:] == ["pull-runtime"]:
            return subprocess.CompletedProcess(command, 0, "runtime ready\n", "")
        for group, pages in self.pages_by_group.items():
            for index in range(pages):
                page = (
                    self.data_root
                    / group
                    / "render_markdown"
                    / "source"
                    / f"uuid-{index}"
                    / "all.md"
                )
                page.parent.mkdir(parents=True, exist_ok=True)
                page.write_text("# chat\n")
        if self.returncode is not None:
            returncode = self.returncode
        else:
            returncode = 0 if '"failed"' not in self.stderr else 2
        return subprocess.CompletedProcess(
            command, returncode, self.stdout, self.stderr
        )


def _streaming(
    run: Callable[..., subprocess.CompletedProcess],
) -> Callable[[Sequence[str], Callable[[str], None]], subprocess.CompletedProcess]:
    """A ``stream`` for ``sync`` built from a ``run`` fake: it runs the command, then hands over its stderr one line
    at a time, as the real ``stream_process`` does while the command runs."""

    def stream(
        command: Sequence[str], on_stderr_line: Callable[[str], None]
    ) -> subprocess.CompletedProcess:
        result = run(command, capture_output=True, text=True)
        for line in result.stderr.splitlines(keepends=True):
            on_stderr_line(line)
        return result

    return stream


def _installed(home: Path) -> import_chats.DatalibInstall:
    install = import_chats.DatalibInstall(home=home)
    install.release_dir.mkdir(parents=True)
    for name in ("datalib-dag", "datalib-step"):
        binary = install.binary(name)
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
    return install


@dataclass(frozen=True)
class _SyncWorkspace:
    """What a sync works on: an installed release, the datalib store and the status file."""

    install: import_chats.DatalibInstall
    data_root: Path
    status_path: Path

    def sync(
        self,
        named: Sequence[import_chats.ChatSource],
        run: Callable[..., subprocess.CompletedProcess],
        stream: Callable[
            [Sequence[str], Callable[[str], None]], subprocess.CompletedProcess
        ]
        | None = None,
        progress_interval_seconds: float = import_chats.PROGRESS_INTERVAL_SECONDS,
    ) -> bool:
        """Sync ``named`` with ``run`` standing in for the release's binaries (and, unless ``stream`` is given,
        for the streamed ``datalib-dag`` run too)."""
        return import_chats.sync(
            named,
            self.install,
            self.data_root,
            self.status_path,
            run,
            stream=_streaming(run) if stream is None else stream,
            progress_interval_seconds=progress_interval_seconds,
        )

    def record(self, key: str) -> dict:
        return import_chats.read_status(self.status_path)["sources"][key]

    def watch_record(self, key: str, is_reached: Callable[[dict], bool]) -> list[dict]:
        """Every reading of ``key``'s record, every 10ms for up to 5s, until one ``is_reached``: how a test sees what
        a running sync's progress thread records."""
        readings: list[dict] = []
        pause = threading.Event()
        for _ in range(500):
            readings.append(self.record(key))
            if is_reached(readings[-1]):
                break
            pause.wait(0.01)
        return readings


@pytest.fixture
def workspace(tmp_path: Path) -> _SyncWorkspace:
    return _SyncWorkspace(
        install=_installed(tmp_path / "home"),
        data_root=tmp_path / "datalib",
        status_path=tmp_path / "import-chats" / "status.json",
    )


def test_the_config_names_every_source_and_feeds_each_into_the_shared_grid_and_search() -> (
    None
):
    config = tomllib.loads(import_chats.render_config([CLAUDE, CHATGPT]))

    assert [group["id"] for group in config["groups"]] == [
        "claude_chats",
        "chatgpt_chats",
        "unified_index",
    ]
    assert [group.get("type") for group in config["groups"]] == [
        "claude",
        "chatgpt",
        None,
    ]
    steps = {(step["group"], step["function"]): step for step in config["steps"]}
    assert steps[("claude_chats", "ingest")]["params"] == {"api": {}}
    assert steps[("chatgpt_chats", "render_markdown")]["inputs"] == [
        "chatgpt_chats/ingest"
    ]
    assert ("claude_chats", "embed") not in steps
    assert steps[("unified_index", "grid_index")]["inputs"] == [
        "claude_chats/render_markdown",
        "chatgpt_chats/render_markdown",
    ]
    assert steps[("unified_index", "qmd_aggregator")]["inputs"] == [
        "claude_chats/keyword_index",
        "chatgpt_chats/keyword_index",
    ]
    assert config["applets"] == [
        {
            "group": "unified_index",
            "id": "unified_index",
            "command": "datalib-applet unified_index",
        }
    ]


def test_a_config_needs_a_source() -> None:
    with pytest.raises(import_chats.ImportChatsError):
        import_chats.render_config([])


def test_a_later_import_of_one_source_keeps_the_ones_imported_before() -> None:
    status = {"sources": {"chatgpt": {"state": "imported"}}}

    assert import_chats.configured_sources(status, [CLAUDE]) == [CLAUDE, CHATGPT]
    assert import_chats.configured_sources({"sources": {}}, [CHATGPT]) == [CHATGPT]


def test_the_run_summary_is_read_from_the_last_summary_line_among_the_event_stream() -> (
    None
):
    output = "\n".join(
        [
            '{"event":"step_start","step":"claude_chats/ingest"}',
            "not json at all",
            _run_summary({"step": "claude_chats/ingest", "status": "succeeded"}),
        ]
    )

    assert import_chats.parse_run_summary(output) == {
        "claude_chats/ingest": {"step": "claude_chats/ingest", "status": "succeeded"}
    }
    assert import_chats.parse_run_summary("no events here") == {}


@pytest.mark.parametrize(
    ("error", "failure_kind", "state"),
    [
        (
            "caused by: list orgs: claude: latchkey curl exit 1: No credentials found for claude-ai.",
            None,
            "needs_sign_in",
        ),
        ("caused by: chatgpt: HTTP 401 token_expired", None, "needs_sign_in"),
        (
            "caused by: No service matches URL: https://chatgpt.com/backend-api/me",
            None,
            "needs_sign_in",
        ),
        ("caused by: session no longer valid", "auth", "needs_sign_in"),
        ("caused by: disk full", "data", "failed"),
        ("caused by: disk full", None, "failed"),
    ],
)
def test_a_failed_ingest_that_a_sign_in_fixes_is_told_apart_from_any_other_failure(
    error: str, failure_kind: str | None, state: str
) -> None:
    assert import_chats.classify_failure(error, failure_kind) == state


def test_the_failure_detail_is_the_caused_by_line_or_else_the_last_line() -> None:
    error = "step exited 1: committed\n[runtime] fetching\nerror: processor\ncaused by: list orgs: refused\n\nmore help"

    assert import_chats.failure_detail(error) == "list orgs: refused"
    assert import_chats.failure_detail("first\nsecond\n") == "second"
    assert import_chats.failure_detail("") == "the import failed"


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "state"),
    [
        (0, '{"uuid": "org"}\n200', "", "connected"),
        (0, '{"uuid": "org"}\n200', "warning: a notice on stderr\n", "connected"),
        (
            1,
            "\n000",
            '{"error": "No credentials found for claude-ai."}\n',
            "needs_permission",
        ),
        (
            1,
            '{"error": "No service matches URL: https://chatgpt.com/backend-api/me"}\n000',
            "",
            "needs_permission",
        ),
        (
            0,
            '{"error": "Request not permitted by the user."}\n403',
            "",
            "needs_permission",
        ),
        (0, '{"detail": "token_expired"}\n401', "", "needs_permission"),
        (0, "<html>Just a moment... cloudflare</html>\n403", "", "blocked"),
        (0, "upstream sad\n502", "", "blocked"),
    ],
)
def test_a_check_says_what_the_agent_does_next(
    returncode: int, stdout: str, stderr: str, state: str
) -> None:
    assert import_chats.classify_check(returncode, stdout, stderr)[0] == state


def test_a_check_asks_the_sources_own_api_through_the_impersonating_gateway() -> None:
    commands: list[list[str]] = []

    def run(command: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess:
        commands.append(list(command))
        return subprocess.CompletedProcess(command, 0, "{}\n200", "")

    assert import_chats.check(CHATGPT, run) == ("connected", "")
    ((command,),) = [commands]
    assert command[:2] == ["latchkey", "curl"]
    assert command[command.index("-H") + 1] == "X-Imbue-Impersonate: 1"
    assert command[-1] == "https://chatgpt.com/backend-api/me"


def test_a_check_reads_an_answer_that_is_not_utf8() -> None:
    def run(_command: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess:
        script = "import sys; sys.stdout.buffer.write(b'<html>\\xff cloudflare</html>\\n403')"
        return subprocess.run([sys.executable, "-c", script], **kwargs)

    assert import_chats.check(CLAUDE, run)[0] == "blocked"


def test_a_sync_writes_the_config_runs_the_named_ingests_and_records_what_it_imported(
    workspace: _SyncWorkspace,
) -> None:
    datalib = _FakeDatalib(
        workspace.data_root,
        _run_summary(
            {"step": "claude_chats/ingest", "status": "succeeded"},
            {"step": "chatgpt_chats/ingest", "status": "succeeded"},
        ),
        {"claude_chats": 3, "chatgpt_chats": 2},
    )

    is_imported = workspace.sync([CLAUDE, CHATGPT], datalib)

    assert is_imported is True
    pull, dag = datalib.commands
    assert pull[1:] == ["pull-runtime"]
    config_path = workspace.data_root / "config.toml"
    assert dag[1:] == [
        str(config_path),
        "--sync",
        "claude_chats/ingest,chatgpt_chats/ingest",
        "--by",
        "import-chats",
    ]
    assert tomllib.loads(config_path.read_text())["groups"][1]["id"] == "chatgpt_chats"
    sources = import_chats.read_status(workspace.status_path)["sources"]
    assert {
        key: (record["state"], record["conversations"], record["pid"])
        for key, record in sources.items()
    } == {
        "claude": ("imported", 3, None),
        "chatgpt": ("imported", 2, None),
    }


def test_a_sync_records_a_sign_in_problem_for_one_source_and_success_for_the_other(
    workspace: _SyncWorkspace,
) -> None:
    datalib = _FakeDatalib(
        workspace.data_root,
        _run_summary(
            {"step": "claude_chats/ingest", "status": "succeeded"},
            {
                "step": "chatgpt_chats/ingest",
                "status": "failed",
                "error": "step exited 1\ncaused by: chatgpt: HTTP 401 token_expired\n",
            },
        ),
        {"claude_chats": 1},
    )

    assert workspace.sync([CLAUDE, CHATGPT], datalib) is False

    assert workspace.record("claude")["state"] == "imported"
    chatgpt = workspace.record("chatgpt")
    assert (chatgpt["state"], chatgpt["detail"]) == (
        "needs_sign_in",
        "chatgpt: HTTP 401 token_expired",
    )


def test_a_sync_records_a_render_failure_and_counts_a_source_with_nothing_new_as_imported(
    workspace: _SyncWorkspace,
) -> None:
    datalib = _FakeDatalib(
        workspace.data_root,
        _run_summary(
            {"step": "claude_chats/ingest", "status": "succeeded"},
            {
                "step": "claude_chats/render_markdown",
                "status": "failed",
                "failure": "data",
                "error": "step exited 1\ncaused by: render store: disk full\n",
            },
            {"step": "chatgpt_chats/ingest", "status": "skipped_up_to_date"},
            {"step": "chatgpt_chats/render_markdown", "status": "skipped_up_to_date"},
        ),
        {},
    )

    assert workspace.sync([CLAUDE, CHATGPT], datalib) is False

    claude = workspace.record("claude")
    assert (claude["state"], claude["detail"]) == ("failed", "render store: disk full")
    assert workspace.record("chatgpt")["state"] == "imported"


def test_a_sync_that_never_summarised_records_the_failure_and_its_output(
    workspace: _SyncWorkspace,
) -> None:
    datalib = _FakeDatalib(
        workspace.data_root,
        '{"event":"run_plan","steps":[]}\nError: the run store is locked\n',
        {},
        stdout="claude_chats/ingest  Running\n",
        returncode=1,
    )

    assert workspace.sync([CLAUDE], datalib) is False

    record = workspace.record("claude")
    assert (record["state"], record["detail"]) == (
        "failed",
        "Error: the run store is locked",
    )


def test_a_sync_that_followed_another_ones_run_to_the_end_records_what_it_imported(
    workspace: _SyncWorkspace,
) -> None:
    datalib = _FakeDatalib(
        workspace.data_root,
        "datalib-dag: the loop on datalib is already running (pid 7); following request r1 there\n",
        {"claude_chats": 2},
        stdout="request r1: done\n",
        returncode=0,
    )

    assert workspace.sync([CLAUDE], datalib) is True

    record = workspace.record("claude")
    assert (record["state"], record["conversations"]) == ("imported", 2)


def test_a_sync_marks_its_sources_importing_with_its_pid_while_it_runs(
    workspace: _SyncWorkspace,
) -> None:
    seen: list[dict] = []

    def run(command: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess:
        seen.append(workspace.record("claude"))
        return subprocess.CompletedProcess(
            command,
            0,
            "",
            _run_summary({"step": "claude_chats/ingest", "status": "succeeded"}),
        )

    workspace.sync([CLAUDE], run)

    assert {(record["state"], record["pid"]) for record in seen} == {
        ("importing", os.getpid())
    }


def test_a_sync_whose_install_fails_records_the_failure_and_says_why(
    workspace: _SyncWorkspace,
) -> None:
    def run(command: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(command, 1, "", "no network\n")

    with pytest.raises(import_chats.ImportChatsError, match="no network"):
        workspace.sync([CHATGPT], run)

    record = workspace.record("chatgpt")
    assert record["state"] == "failed"
    assert "no network" in record["detail"]


def test_a_sync_whose_runtime_fetch_hangs_gives_up_and_records_the_failure(
    workspace: _SyncWorkspace,
) -> None:
    def run(
        command: Sequence[str], timeout: float | None = None, **_kwargs: object
    ) -> subprocess.CompletedProcess:
        assert timeout is not None
        raise subprocess.TimeoutExpired(command, timeout)

    with pytest.raises(import_chats.ImportChatsError, match="no answer in"):
        workspace.sync([CLAUDE], run)

    record = workspace.record("claude")
    assert record["state"] == "failed"
    assert "could not fetch its runtime" in record["detail"]


def test_a_sync_that_cannot_run_datalib_records_the_failure_and_reports_it_as_an_import_error(
    workspace: _SyncWorkspace,
) -> None:
    def run(command: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess:
        raise PermissionError(13, "Permission denied", command[0])

    with pytest.raises(import_chats.ImportChatsError, match="Permission denied"):
        workspace.sync([CLAUDE], run)

    record = workspace.record("claude")
    assert record["state"] == "failed"
    assert "Permission denied" in record["detail"]


def test_an_unreadable_status_file_is_an_error_rather_than_a_fresh_start(
    tmp_path: Path,
) -> None:
    status_path = tmp_path / "status.json"
    assert import_chats.read_status(status_path) == {"sources": {}}

    status_path.write_text("{not json")
    with pytest.raises(import_chats.ImportChatsError):
        import_chats.read_status(status_path)

    status_path.write_text("[]")
    with pytest.raises(import_chats.ImportChatsError):
        import_chats.read_status(status_path)


def test_a_record_waits_for_another_writer_and_keeps_what_it_wrote(
    tmp_path: Path,
) -> None:
    status_path = tmp_path / "import-chats" / "status.json"
    with import_chats.status_lock(status_path):
        recorder = threading.Thread(
            target=import_chats.record_source,
            args=(status_path, "claude", "imported", 3, ""),
            kwargs={"pid": None},
        )
        recorder.start()
        recorder.join(timeout=0.2)
        assert recorder.is_alive()
        import_chats.write_status(
            status_path, {"sources": {"chatgpt": {"state": "importing"}}}
        )
    recorder.join()

    sources = import_chats.read_status(status_path)["sources"]
    assert (sources["claude"]["state"], sources["chatgpt"]["state"]) == (
        "imported",
        "importing",
    )


def test_the_install_picks_the_build_for_this_machine_and_refuses_one_it_has_none_for() -> (
    None
):
    assert import_chats.linux_arch("Linux", "x86_64") == "x86_64"
    assert import_chats.linux_arch("Linux", "arm64") == "aarch64"
    with pytest.raises(import_chats.ImportChatsError, match="only on Linux"):
        import_chats.linux_arch("Darwin", "arm64")
    with pytest.raises(import_chats.ImportChatsError, match="only on Linux"):
        import_chats.linux_arch("Linux", "riscv64")


def _tarball(path: Path, members: Sequence[str]) -> Path:
    with tarfile.open(path, "w:gz") as archive:
        for member in members:
            info = tarfile.TarInfo(member)
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            archive.addfile(info)
    return path


def test_the_release_unpacks_to_its_one_directory_and_anything_else_is_refused(
    tmp_path: Path,
) -> None:
    triple = "x86_64-unknown-linux-musl"
    good = _tarball(tmp_path / "good.tar.gz", [f"datalib-v0.40.0-{triple}"])
    assert (
        import_chats.unpack_release(good, tmp_path / "good", triple)
        == tmp_path / "good" / f"datalib-v0.40.0-{triple}"
    )

    empty = _tarball(tmp_path / "empty.tar.gz", ["something-else"])
    with pytest.raises(import_chats.ImportChatsError, match="holds 0"):
        import_chats.unpack_release(empty, tmp_path / "empty", triple)

    corrupt = tmp_path / "corrupt.tar.gz"
    corrupt.write_bytes(b"not a tarball")
    with pytest.raises(import_chats.ImportChatsError, match="could not be unpacked"):
        import_chats.unpack_release(corrupt, tmp_path / "corrupt", triple)


def test_a_check_without_latchkey_says_so() -> None:
    def run(command: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess:
        raise FileNotFoundError(2, "No such file or directory", command[0])

    with pytest.raises(import_chats.ImportChatsError, match="cannot run latchkey"):
        import_chats.check(CLAUDE, run)


def _rendered_page(
    data_root: Path,
    group: str,
    uuid: str,
    title: str,
    original_url: str,
    message_times: Sequence[str],
) -> Path:
    """A page laid out as datalib's chat renderer writes one: frontmatter, a title with its source link, and one
    timestamped section per message."""
    page = data_root / group / "render_markdown" / "account" / uuid / "all.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    messages = "\n".join(
        f'<div class="msg"><h2><time class="msg-ts" datetime="{when}" title="{when}">{when}</time></h2>Hi</div>'
        for when in message_times
    )
    quoted = '"' + title.replace('"', '\\"') + '"'
    page.write_text(
        "---\n"
        f"title: {quoted}\n"
        "provider: claude\n"
        f"chat_uuid: {uuid}\n"
        f"display: {quoted}\n"
        "item_count: 2\n"
        "---\n\n"
        f'<h1 class="page-title">{title} <a class="source-link" href="{original_url}" target="_blank">x</a></h1>\n\n'
        f"{messages}\n"
    )
    return page


def test_a_page_is_read_for_its_title_its_original_and_its_latest_message(
    tmp_path: Path,
) -> None:
    path = _rendered_page(
        tmp_path,
        "claude_chats",
        "u1",
        'Drafting a "greeting" in C:\\temp\\new with \\frac{1}{2}',
        "https://claude.ai/chat/c1",
        ["2026-09-30T10:00:00+00:00", "2026-10-02T09:00:00+02:00", "not a time"],
    )

    page = import_chats.read_page(path)

    assert page.title == 'Drafting a "greeting" in C:\\temp\\new with \\frac{1}{2}'
    assert page.original_url == "https://claude.ai/chat/c1"
    assert page.last_message_at is not None
    assert page.last_message_at.isoformat() == "2026-10-02T09:00:00+02:00"
    assert page.is_project is False
    two_lines = _rendered_page(
        tmp_path, "claude_chats", "u2", "First line\nsecond line", "", []
    )
    assert import_chats.read_page(two_lines).title == "First line"


def test_a_page_without_frontmatter_or_times_is_listed_by_its_directory(
    tmp_path: Path,
) -> None:
    path = tmp_path / "u9" / "all.md"
    path.parent.mkdir()
    path.write_text("# chat\n")

    page = import_chats.read_page(path)

    assert (page.title, page.original_url, page.last_message_at) == ("u9", "", None)


def test_the_index_lists_chats_by_month_newest_first_then_undated_then_projects(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "datalib"
    index_path = tmp_path / "import-chats" / "claude-chats.md"
    _rendered_page(
        data_root,
        "claude_chats",
        "old",
        "Older [draft] <T> in C:\\",
        "https://claude.ai/chat/a",
        ["2026-09-03T08:00:00+00:00"],
    )
    _rendered_page(
        data_root,
        "claude_chats",
        "new",
        "Newest",
        "https://claude.ai/chat/b",
        ["2026-10-07T08:00:00+00:00"],
    )
    _rendered_page(
        data_root,
        "claude_chats",
        "mid",
        "Middle",
        "https://claude.ai/chat/c",
        ["2026-10-01T08:00:00+00:00"],
    )
    _rendered_page(
        data_root, "claude_chats", "nodate", "No dates", "https://claude.ai/chat/d", []
    )
    _rendered_page(
        data_root,
        "claude_chats",
        "proj",
        "Stellar Cartography",
        "https://claude.ai/project/p",
        [],
    )

    import_chats.write_index(data_root, CLAUDE, index_path)

    lines = index_path.read_text().splitlines()
    assert lines[0] == "# Claude chats"
    assert lines[2].startswith("4 conversations, most recent first.")
    assert [line for line in lines if line.startswith("## ")] == [
        "## October 2026",
        "## September 2026",
        "## Undated",
        "## Projects",
    ]
    entries = [line for line in lines if line.startswith("- ")]
    assert entries == [
        "- 2026-10-07 · [Newest](../datalib/claude_chats/render_markdown/account/new/all.md) · [original](https://claude.ai/chat/b)",
        "- 2026-10-01 · [Middle](../datalib/claude_chats/render_markdown/account/mid/all.md) · [original](https://claude.ai/chat/c)",
        "- 2026-09-03 · [Older \\[draft\\] \\<T> in C:\\\\](../datalib/claude_chats/render_markdown/account/old/all.md) · [original](https://claude.ai/chat/a)",
        "- [No dates](../datalib/claude_chats/render_markdown/account/nodate/all.md) · [original](https://claude.ai/chat/d)",
        "- [Stellar Cartography](../datalib/claude_chats/render_markdown/account/proj/all.md) · [original](https://claude.ai/project/p)",
    ]
    assert list(index_path.parent.iterdir()) == [index_path]


def test_an_index_of_a_source_with_nothing_rendered_says_so(tmp_path: Path) -> None:
    index_path = tmp_path / "chatgpt-chats.md"

    import_chats.write_index(tmp_path / "datalib", CHATGPT, index_path)

    assert index_path.read_text().splitlines()[:3] == [
        "# ChatGPT chats",
        "",
        '0 conversations, most recent first. Each title opens the copy in this workspace; "original" opens it in ChatGPT.',
    ]


def test_a_sync_rewrites_the_index_of_each_source_it_synced(
    workspace: _SyncWorkspace,
) -> None:
    datalib = _FakeDatalib(
        workspace.data_root,
        _run_summary(
            {"step": "claude_chats/ingest", "status": "succeeded"},
            {
                "step": "chatgpt_chats/ingest",
                "status": "failed",
                "error": "caused by: HTTP 401",
            },
        ),
        {"claude_chats": 2},
    )

    workspace.sync([CLAUDE, CHATGPT], datalib)

    index_dir = workspace.status_path.parent
    assert "2 conversations" in (index_dir / "claude-chats.md").read_text()
    assert "0 conversations" in (index_dir / "chatgpt-chats.md").read_text()


def test_a_sync_whose_index_cannot_be_written_still_records_what_it_imported(
    workspace: _SyncWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    datalib = _FakeDatalib(
        workspace.data_root,
        _run_summary({"step": "claude_chats/ingest", "status": "succeeded"}),
        {"claude_chats": 2},
    )
    index_path = import_chats.index_path_for(workspace.status_path, CLAUDE)
    index_path.mkdir(parents=True)

    assert workspace.sync([CLAUDE], datalib) is True

    record = workspace.record("claude")
    assert (record["state"], record["conversations"]) == ("imported", 2)
    assert f"their index {index_path} could not be rewritten" in capsys.readouterr().err
    assert not list(index_path.parent.glob("*.tmp-*"))


def test_a_long_title_with_tabs_is_one_line_cut_short_in_the_index(
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "datalib"
    index_path = tmp_path / "chatgpt-chats.md"
    _rendered_page(
        data_root,
        "chatgpt_chats",
        "u1",
        "word\t  " * 100,
        "https://chatgpt.com/c/x",
        ["2026-10-01T08:00:00+00:00"],
    )

    import_chats.write_index(data_root, CHATGPT, index_path)

    (entry,) = [
        line for line in index_path.read_text().splitlines() if line.startswith("- ")
    ]
    title = entry.split("[", 1)[1].split("](", 1)[0]
    assert len(title) == 120
    assert title.startswith("word word ")
    assert title.endswith("word…")
    assert entry.endswith(" · [original](https://chatgpt.com/c/x)")


def test_a_running_sync_records_the_pages_rendered_so_far(
    workspace: _SyncWorkspace,
) -> None:
    seen_counts: list[int] = []

    def run(command: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess:
        if command[1:] == ["pull-runtime"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        seen_counts.append(workspace.record("chatgpt")["conversations"])
        for index in range(3):
            _rendered_page(
                workspace.data_root,
                "chatgpt_chats",
                f"u{index}",
                f"Chat {index}",
                "https://chatgpt.com/c/x",
                [],
            )
        seen_counts.extend(
            record["conversations"]
            for record in workspace.watch_record(
                "chatgpt",
                lambda record: (
                    record["state"] == "importing" and record["conversations"] == 3
                ),
            )
        )
        summary = _run_summary({"step": "chatgpt_chats/ingest", "status": "succeeded"})
        return subprocess.CompletedProcess(command, 0, "", summary)

    workspace.sync([CHATGPT], run, progress_interval_seconds=0.01)

    assert seen_counts[0] == 0
    assert seen_counts[-1] == 3
    assert workspace.record("chatgpt")["state"] == "imported"


def _progress_event(event: str, step: str, **fields: object) -> str:
    return json.dumps({"event": event, "step": step, **fields}) + "\n"


def test_ingest_progress_follows_each_ingests_length_and_increments() -> None:
    progress = import_chats.IngestProgress()
    assert progress.of(CHATGPT) is None

    for line in [
        "not an event\n",
        _progress_event("progress_length", "chatgpt_chats/render_markdown", total=9),
        _progress_event("progress_inc", "chatgpt_chats/ingest", delta=1),
        _progress_event("progress_length", "chatgpt_chats/ingest", total=3),
        _progress_event("progress_inc", "chatgpt_chats/ingest", delta=1),
        _progress_event("progress_message", "chatgpt_chats/ingest", msg="conv-1"),
        _progress_event("progress_inc", "chatgpt_chats/ingest", delta=4),
    ]:
        progress.observe(line)

    assert progress.of(CHATGPT) == import_chats.FetchProgress(fetched=3, total=3)
    assert progress.of(CLAUDE) is None


def test_a_retried_ingest_counts_its_progress_from_zero() -> None:
    progress = import_chats.IngestProgress()
    for line in [
        _progress_event("step_start", "chatgpt_chats/ingest", attempt=1),
        _progress_event("progress_length", "chatgpt_chats/ingest", total=582),
        _progress_event("progress_inc", "chatgpt_chats/ingest", delta=300),
        _progress_event("step_start", "chatgpt_chats/ingest", attempt=2),
    ]:
        progress.observe(line)
    assert progress.of(CHATGPT) is None

    for line in [
        _progress_event("progress_length", "chatgpt_chats/ingest", total=282),
        _progress_event("progress_inc", "chatgpt_chats/ingest", delta=1),
    ]:
        progress.observe(line)
    assert progress.of(CHATGPT) == import_chats.FetchProgress(fetched=1, total=282)


def test_stream_process_hands_over_stderr_lines_as_they_come_and_keeps_both_streams() -> (
    None
):
    seen: list[str] = []
    script = "import sys; print('report'); [print(f'line {i}', file=sys.stderr, flush=True) for i in range(3)]; sys.exit(2)"

    result = import_chats.stream_process([sys.executable, "-c", script], seen.append)

    assert seen == ["line 0\n", "line 1\n", "line 2\n"]
    assert (result.returncode, result.stdout, result.stderr) == (
        2,
        "report\n",
        "line 0\nline 1\nline 2\n",
    )


def test_stream_process_follows_output_that_is_not_utf8_to_the_end() -> None:
    script = "import sys; sys.stderr.buffer.write(b'bad \\xff byte\\nnext\\n')"

    result = import_chats.stream_process(
        [sys.executable, "-c", script], lambda _line: None
    )

    assert result.stderr == "bad � byte\nnext\n"


def test_stream_process_stops_the_command_when_following_it_fails() -> None:
    script = "import os, sys, time; print(os.getpid(), file=sys.stderr, flush=True); time.sleep(60)"
    pids: list[int] = []

    def fail(line: str) -> None:
        pids.append(int(line))
        raise RuntimeError("could not follow the command")

    with pytest.raises(RuntimeError, match="could not follow"):
        import_chats.stream_process([sys.executable, "-c", script], fail)

    (pid,) = pids
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_a_running_sync_records_how_many_of_its_conversations_it_has_fetched(
    workspace: _SyncWorkspace,
) -> None:
    seen: list[tuple[object, object]] = []

    def stream(
        command: Sequence[str], on_stderr_line: Callable[[str], None]
    ) -> subprocess.CompletedProcess:
        for line in [
            _progress_event("progress_length", "chatgpt_chats/ingest", total=5),
            _progress_event("progress_inc", "chatgpt_chats/ingest", delta=1),
            _progress_event("progress_inc", "chatgpt_chats/ingest", delta=1),
        ]:
            on_stderr_line(line)
        seen.extend(
            (record["fetched"], record["to_fetch"])
            for record in workspace.watch_record(
                "chatgpt",
                lambda record: (record["fetched"], record["to_fetch"]) == (2, 5),
            )
        )
        summary = _run_summary({"step": "chatgpt_chats/ingest", "status": "succeeded"})
        return subprocess.CompletedProcess(command, 0, "", summary)

    workspace.sync(
        [CHATGPT],
        _FakeDatalib(workspace.data_root, "", {}),
        stream=stream,
        progress_interval_seconds=0.01,
    )

    assert seen[-1] == (2, 5)
    final = workspace.record("chatgpt")
    assert (final["state"], final["fetched"], final["to_fetch"]) == (
        "imported",
        None,
        None,
    )

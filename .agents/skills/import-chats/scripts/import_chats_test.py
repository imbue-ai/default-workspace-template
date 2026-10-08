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
from collections.abc import Sequence
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


def _installed(home: Path) -> object:
    install = import_chats.DatalibInstall(home=home)
    install.release_dir.mkdir(parents=True)
    for name in ("datalib-dag", "datalib-step"):
        binary = install.binary(name)
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
    return install


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


def test_a_sync_writes_the_config_runs_the_named_ingests_and_records_what_it_imported(
    tmp_path: Path,
) -> None:
    install = _installed(tmp_path / "home")
    data_root = tmp_path / "data" / ".skills" / "datalib"
    status_path = tmp_path / "status.json"
    datalib = _FakeDatalib(
        data_root,
        _run_summary(
            {"step": "claude_chats/ingest", "status": "succeeded"},
            {"step": "chatgpt_chats/ingest", "status": "succeeded"},
        ),
        {"claude_chats": 3, "chatgpt_chats": 2},
    )

    is_imported = import_chats.sync(
        [CLAUDE, CHATGPT], install, data_root, status_path, datalib
    )

    assert is_imported is True
    pull, dag = datalib.commands
    assert pull[1:] == ["pull-runtime"]
    assert dag[1:] == [
        str(data_root / "config.toml"),
        "--sync",
        "claude_chats/ingest,chatgpt_chats/ingest",
        "--by",
        "import-chats",
    ]
    assert (
        tomllib.loads((data_root / "config.toml").read_text())["groups"][1]["id"]
        == "chatgpt_chats"
    )
    sources = import_chats.read_status(status_path)["sources"]
    assert {
        key: (record["state"], record["conversations"], record["pid"])
        for key, record in sources.items()
    } == {
        "claude": ("imported", 3, None),
        "chatgpt": ("imported", 2, None),
    }


def test_a_sync_records_a_sign_in_problem_for_one_source_and_success_for_the_other(
    tmp_path: Path,
) -> None:
    install = _installed(tmp_path / "home")
    data_root = tmp_path / "datalib"
    status_path = tmp_path / "status.json"
    datalib = _FakeDatalib(
        data_root,
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

    assert (
        import_chats.sync([CLAUDE, CHATGPT], install, data_root, status_path, datalib)
        is False
    )

    sources = import_chats.read_status(status_path)["sources"]
    assert sources["claude"]["state"] == "imported"
    assert (sources["chatgpt"]["state"], sources["chatgpt"]["detail"]) == (
        "needs_sign_in",
        "chatgpt: HTTP 401 token_expired",
    )


def test_a_sync_whose_pages_did_not_render_records_the_render_failure(
    tmp_path: Path,
) -> None:
    install = _installed(tmp_path / "home")
    data_root = tmp_path / "datalib"
    status_path = tmp_path / "status.json"
    datalib = _FakeDatalib(
        data_root,
        _run_summary(
            {"step": "claude_chats/ingest", "status": "succeeded"},
            {
                "step": "claude_chats/render_markdown",
                "status": "failed",
                "failure": "data",
                "error": "step exited 1\ncaused by: render store: disk full\n",
            },
            {"step": "chatgpt_chats/ingest", "status": "succeeded"},
            {"step": "chatgpt_chats/render_markdown", "status": "skipped_up_to_date"},
        ),
        {},
    )

    assert (
        import_chats.sync([CLAUDE, CHATGPT], install, data_root, status_path, datalib)
        is False
    )

    sources = import_chats.read_status(status_path)["sources"]
    assert (sources["claude"]["state"], sources["claude"]["detail"]) == (
        "failed",
        "render store: disk full",
    )
    assert sources["chatgpt"]["state"] == "imported"


def test_a_sync_that_never_summarised_records_the_failure_and_its_output(
    tmp_path: Path,
) -> None:
    install = _installed(tmp_path / "home")
    data_root = tmp_path / "datalib"
    status_path = tmp_path / "status.json"
    datalib = _FakeDatalib(
        data_root,
        '{"event":"run_plan","steps":[]}\nError: the run store is locked\n',
        {},
        stdout="claude_chats/ingest  Running\n",
        returncode=1,
    )

    assert (
        import_chats.sync([CLAUDE], install, data_root, status_path, datalib) is False
    )

    record = import_chats.read_status(status_path)["sources"]["claude"]
    assert (record["state"], record["detail"]) == (
        "failed",
        "Error: the run store is locked",
    )


def test_a_sync_that_followed_another_ones_run_to_the_end_records_what_it_imported(
    tmp_path: Path,
) -> None:
    install = _installed(tmp_path / "home")
    data_root = tmp_path / "datalib"
    status_path = tmp_path / "status.json"
    datalib = _FakeDatalib(
        data_root,
        "datalib-dag: the loop on datalib is already running (pid 7); following request r1 there\n",
        {"claude_chats": 2},
        stdout="request r1: done\n",
        returncode=0,
    )

    assert import_chats.sync([CLAUDE], install, data_root, status_path, datalib) is True

    record = import_chats.read_status(status_path)["sources"]["claude"]
    assert (record["state"], record["conversations"]) == ("imported", 2)


def test_a_sync_marks_its_sources_importing_with_its_pid_while_it_runs(
    tmp_path: Path,
) -> None:
    install = _installed(tmp_path / "home")
    status_path = tmp_path / "status.json"
    seen: list[dict] = []

    def run(command: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess:
        seen.append(import_chats.read_status(status_path)["sources"]["claude"])
        return subprocess.CompletedProcess(
            command,
            0,
            "",
            _run_summary({"step": "claude_chats/ingest", "status": "succeeded"}),
        )

    import_chats.sync([CLAUDE], install, tmp_path / "datalib", status_path, run)

    assert {(record["state"], record["pid"]) for record in seen} == {
        ("importing", os.getpid())
    }


def test_a_sync_whose_install_fails_records_the_failure_and_says_why(
    tmp_path: Path,
) -> None:
    install = _installed(tmp_path / "home")
    status_path = tmp_path / "status.json"

    def run(command: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(command, 1, "", "no network\n")

    with pytest.raises(import_chats.ImportChatsError, match="no network"):
        import_chats.sync([CHATGPT], install, tmp_path / "datalib", status_path, run)

    record = import_chats.read_status(status_path)["sources"]["chatgpt"]
    assert record["state"] == "failed"
    assert "no network" in record["detail"]


def test_a_sync_that_cannot_run_datalib_records_the_failure_and_reports_it_as_an_import_error(
    tmp_path: Path,
) -> None:
    install = _installed(tmp_path / "home")
    status_path = tmp_path / "status.json"

    def run(command: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess:
        raise PermissionError(13, "Permission denied", command[0])

    with pytest.raises(import_chats.ImportChatsError, match="Permission denied"):
        import_chats.sync([CLAUDE], install, tmp_path / "datalib", status_path, run)

    record = import_chats.read_status(status_path)["sources"]["claude"]
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


def test_a_sync_rewrites_the_index_of_each_source_it_synced(tmp_path: Path) -> None:
    install = _installed(tmp_path / "home")
    data_root = tmp_path / "datalib"
    status_path = tmp_path / "import-chats" / "status.json"
    datalib = _FakeDatalib(
        data_root,
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

    import_chats.sync([CLAUDE, CHATGPT], install, data_root, status_path, datalib)

    assert (
        "2 conversations" in (tmp_path / "import-chats" / "claude-chats.md").read_text()
    )
    assert (
        "0 conversations"
        in (tmp_path / "import-chats" / "chatgpt-chats.md").read_text()
    )


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


def test_a_running_sync_records_the_pages_rendered_so_far(tmp_path: Path) -> None:
    install = _installed(tmp_path / "home")
    data_root = tmp_path / "datalib"
    status_path = tmp_path / "status.json"
    seen_counts: list[int] = []

    def run(command: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess:
        if command[1:] == ["pull-runtime"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        seen_counts.append(
            import_chats.read_status(status_path)["sources"]["chatgpt"]["conversations"]
        )
        for index in range(3):
            _rendered_page(
                data_root,
                "chatgpt_chats",
                f"u{index}",
                f"Chat {index}",
                "https://chatgpt.com/c/x",
                [],
            )
        pause = threading.Event()
        for _ in range(500):
            record = import_chats.read_status(status_path)["sources"]["chatgpt"]
            seen_counts.append(record["conversations"])
            if record["state"] == "importing" and record["conversations"] == 3:
                break
            pause.wait(0.01)
        summary = _run_summary({"step": "chatgpt_chats/ingest", "status": "succeeded"})
        return subprocess.CompletedProcess(command, 0, "", summary)

    import_chats.sync(
        [CHATGPT], install, data_root, status_path, run, progress_interval_seconds=0.01
    )

    assert seen_counts[0] == 0
    assert seen_counts[-1] == 3
    assert (
        import_chats.read_status(status_path)["sources"]["chatgpt"]["state"]
        == "imported"
    )

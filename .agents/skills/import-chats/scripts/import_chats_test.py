"""Tests for the import-chats skill's script: the config it writes, how it reads datalib's and latchkey's answers,
and the status it records through a sync."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
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
    renders ``pages_by_group`` pages for each source it was asked to sync and answers ``summary``."""

    def __init__(
        self,
        data_root: Path,
        summary: str,
        pages_by_group: dict[str, int],
        stderr: str = "",
    ) -> None:
        self.data_root = data_root
        self.summary = summary
        self.pages_by_group = pages_by_group
        self.stderr = stderr
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
        return subprocess.CompletedProcess(
            command,
            0 if '"failed"' not in self.summary else 2,
            self.summary,
            self.stderr,
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
    ("error", "state"),
    [
        (
            "caused by: list orgs: claude: latchkey curl exit 1: No credentials found for claude-ai.",
            "needs_sign_in",
        ),
        ("caused by: chatgpt: HTTP 401 token_expired", "needs_sign_in"),
        (
            "caused by: No service matches URL: https://chatgpt.com/backend-api/me",
            "needs_sign_in",
        ),
        ("caused by: disk full", "failed"),
    ],
)
def test_a_failed_ingest_that_a_sign_in_fixes_is_told_apart_from_any_other_failure(
    error: str, state: str
) -> None:
    assert import_chats.classify_failure(error) == state


def test_the_failure_detail_is_the_caused_by_line_or_else_the_last_line() -> None:
    error = "step exited 1: committed\n[runtime] fetching\nerror: processor\ncaused by: list orgs: refused\n\nmore help"

    assert import_chats.failure_detail(error) == "list orgs: refused"
    assert import_chats.failure_detail("first\nsecond\n") == "second"
    assert import_chats.failure_detail("") == "the import failed"


@pytest.mark.parametrize(
    ("returncode", "output", "state"),
    [
        (0, '{"uuid": "org"}\n200', "connected"),
        (
            1,
            '{"error": "No credentials found for claude-ai."}\n000',
            "needs_permission",
        ),
        (
            1,
            '{"error": "No service matches URL: https://chatgpt.com/backend-api/me"}\n000',
            "needs_permission",
        ),
        (0, '{"error": "Request not permitted by the user."}\n403', "needs_permission"),
        (0, '{"detail": "token_expired"}\n401', "needs_permission"),
        (0, "<html>Just a moment... cloudflare</html>\n403", "blocked"),
        (0, "upstream sad\n502", "blocked"),
    ],
)
def test_a_check_says_what_the_agent_does_next(
    returncode: int, output: str, state: str
) -> None:
    assert import_chats.classify_check(returncode, output)[0] == state


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


def test_a_sync_that_never_summarised_records_the_failure_and_its_output(
    tmp_path: Path,
) -> None:
    install = _installed(tmp_path / "home")
    data_root = tmp_path / "datalib"
    status_path = tmp_path / "status.json"
    datalib = _FakeDatalib(data_root, "", {}, stderr="config.toml: 1 entry dropped\n")

    assert (
        import_chats.sync([CLAUDE], install, data_root, status_path, datalib) is False
    )

    record = import_chats.read_status(status_path)["sources"]["claude"]
    assert (record["state"], record["detail"]) == (
        "failed",
        "config.toml: 1 entry dropped",
    )


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
            _run_summary({"step": "claude_chats/ingest", "status": "succeeded"}),
            "",
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


def test_a_check_without_latchkey_says_so() -> None:
    def run(command: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess:
        raise FileNotFoundError(2, "No such file or directory", command[0])

    with pytest.raises(import_chats.ImportChatsError, match="cannot run latchkey"):
        import_chats.check(CLAUDE, run)

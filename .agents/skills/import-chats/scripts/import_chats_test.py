"""Tests for the import-chats skill's script: the sources it adds to datalib's config, how it reads datalib's and
latchkey's answers, the index it writes from the raw stores, and the status it records through a sync."""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import threading
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
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

# The raw stores' tables, as datalib's Claude and ChatGPT ingests lay them out (the columns the script reads).
_SCHEMA_BY_GROUP = {
    "claude_chats": (
        "CREATE TABLE conversations (id TEXT PRIMARY KEY, payload TEXT, org_uuid TEXT, org_name TEXT, name TEXT, "
        "updated_at TEXT)",
        "CREATE TABLE projects (id TEXT PRIMARY KEY, payload TEXT, org_uuid TEXT, org_name TEXT, name TEXT, "
        "updated_at TEXT)",
    ),
    "chatgpt_chats": (
        "CREATE TABLE conversations (id TEXT PRIMARY KEY, payload TEXT, title TEXT, update_time TEXT)",
    ),
}


def _raw_store(
    data_root: Path,
    source: import_chats.ChatSource,
    conversations: Sequence[tuple[str, str | None, str | None]] = (),
    projects: Sequence[tuple[str, str | None, str | None]] = (),
    unfetched: Sequence[str] = (),
) -> Path:
    """A raw store holding ``conversations`` (and, for Claude, ``projects``) as ``(id, title, updated_at)``, plus
    ``unfetched`` conversation ids that were listed but whose detail has not arrived (no payload)."""
    store = data_root / source.group / "ingest" / "entities.doltlite_db"
    store.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(store)) as db:
        for ddl in _SCHEMA_BY_GROUP[source.group]:
            db.execute(ddl.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS"))
        title_column, updated_column = (
            ("name", "updated_at") if source is CLAUDE else ("title", "update_time")
        )
        for row_id, title, updated in conversations:
            db.execute(
                f"INSERT OR REPLACE INTO conversations (id, payload, {title_column}, {updated_column}) "
                "VALUES (?, '{}', ?, ?)",
                (row_id, title, updated),
            )
        for row_id in unfetched:
            db.execute(
                "INSERT OR REPLACE INTO conversations (id) VALUES (?)", (row_id,)
            )
        for row_id, title, updated in projects:
            db.execute(
                "INSERT OR REPLACE INTO projects (id, payload, name, updated_at) VALUES (?, '{}', ?, ?)",
                (row_id, title, updated),
            )
        db.commit()
    return store


def _run_summary(*steps: dict) -> str:
    return json.dumps({"event": "run_summary", "steps": list(steps)})


@dataclass
class _FakeDatalib:
    """Stands in for the datalib installer and binaries.

    The installer succeeds; ``datalib-dag --check`` answers ``check_returncode``; ``turn-off`` succeeds unless its
    step is in ``turn_off_failures``; a ``datalib-dag`` sync calls ``on_sync`` (where a test writes what the ingest
    would), then writes ``stderr`` (the event stream, where the real binary writes its run summary) and ``stdout``,
    exiting 2 when a step failed, like the real binary, unless ``returncode`` says otherwise.
    ``datalib-doltlite -readonly -json <store> <sql>`` runs the query on the store with sqlite3 and prints it as the
    real one does, nothing at all for no rows."""

    stderr: str = ""
    stdout: str = ""
    returncode: int | None = None
    check_returncode: int = 0
    turn_off_failures: Sequence[str] = ()
    on_sync: Callable[[], None] = lambda: None
    commands: list[list[str]] = field(default_factory=list)

    def __call__(
        self, command: Sequence[str], **_kwargs: object
    ) -> subprocess.CompletedProcess:
        command = list(command)
        self.commands.append(command)
        name = Path(command[0]).name
        if name == "bash":
            return subprocess.CompletedProcess(
                command, 0, "[install_datalib] in place\n", ""
            )
        if name == "datalib-doltlite":
            return self._query(command)
        assert name == "datalib-dag", command
        if command[1] == "--check":
            return subprocess.CompletedProcess(
                command, self.check_returncode, "", "config.toml:3:1: error: no\n"
            )
        if command[1] == "turn-off":
            if command[3] in self.turn_off_failures:
                return subprocess.CompletedProcess(
                    command, 1, "", f'Error: no step "{command[3]}"\n'
                )
            return subprocess.CompletedProcess(
                command, 0, f"turned off {command[3]}\n", ""
            )
        self.on_sync()
        returncode = (
            self.returncode
            if self.returncode is not None
            else (2 if '"failed"' in self.stderr else 0)
        )
        return subprocess.CompletedProcess(
            command, returncode, self.stdout, self.stderr
        )

    @property
    def syncs(self) -> list[list[str]]:
        return [
            c
            for c in self.commands
            if Path(c[0]).name == "datalib-dag" and c[1] not in ("--check", "turn-off")
        ]

    @property
    def turned_off(self) -> list[str]:
        return [
            c[3]
            for c in self.commands
            if Path(c[0]).name == "datalib-dag" and c[1] == "turn-off"
        ]

    @staticmethod
    def _query(command: list[str]) -> subprocess.CompletedProcess:
        assert command[1:3] == ["-readonly", "-json"], command
        store, sql = command[3], command[4]
        with contextlib.closing(
            sqlite3.connect(f"file:{store}?mode=ro", uri=True)
        ) as db:
            db.row_factory = sqlite3.Row
            rows = [dict(row) for row in db.execute(sql)]
        return subprocess.CompletedProcess(
            command, 0, json.dumps(rows) if rows else "", ""
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


def _wait_until(is_reached: Callable[[], bool]) -> bool:
    """Check ``is_reached`` every 10ms for up to 5s; whether it was: how a test waits on a running sync's
    progress thread."""
    pause = threading.Event()
    for _ in range(500):
        if is_reached():
            return True
        pause.wait(0.01)
    return False


@dataclass(frozen=True)
class _SyncWorkspace:
    """What a sync works on: the linked binaries, the datalib store and the status file."""

    datalib: import_chats.Datalib
    status_path: Path

    @property
    def data_root(self) -> Path:
        return self.datalib.data_root

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
        """Sync ``named`` with ``run`` standing in for the installer and binaries (and, unless ``stream`` is given,
        for the streamed ``datalib-dag`` run too)."""
        return import_chats.sync(
            named,
            self.datalib,
            Path("install_datalib.sh"),
            self.status_path,
            run,
            stream=_streaming(run) if stream is None else stream,
            progress_interval_seconds=progress_interval_seconds,
        )

    def record(self, key: str) -> dict:
        return import_chats.read_status(self.status_path)["sources"][key]

    def watch_record(self, key: str, is_reached: Callable[[dict], bool]) -> list[dict]:
        """Every reading of ``key``'s record until one ``is_reached``: how a test sees what a running sync's progress
        thread records."""
        readings: list[dict] = []

        def is_latest_reached() -> bool:
            readings.append(self.record(key))
            return is_reached(readings[-1])

        _wait_until(is_latest_reached)
        return readings


@pytest.fixture
def workspace(tmp_path: Path) -> _SyncWorkspace:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("datalib-dag", "datalib-doltlite"):
        (bin_dir / name).write_text("#!/bin/sh\n")
    return _SyncWorkspace(
        datalib=import_chats.Datalib(bin_dir=bin_dir, data_root=tmp_path / "datalib"),
        status_path=tmp_path / "import-chats" / "status.json",
    )


# --- the config ----------------------------------------------------------------------------------------------------


def test_a_sources_stanza_downloads_over_the_api_and_carries_its_search_steps_unwired() -> (
    None
):
    config = tomllib.loads(
        import_chats.source_stanza(CLAUDE) + "\n" + import_chats.source_stanza(CHATGPT)
    )

    assert [(group["id"], group["type"]) for group in config["groups"]] == [
        ("claude_chats", "claude"),
        ("chatgpt_chats", "chatgpt"),
    ]
    steps = {(step["group"], step["function"]): step for step in config["steps"]}
    assert set(steps) == {
        (group, function)
        for group in ("claude_chats", "chatgpt_chats")
        for function in ("ingest", "render_markdown", "keyword_index")
    }
    assert steps[("claude_chats", "ingest")]["params"] == {"api": {}}
    assert steps[("chatgpt_chats", "render_markdown")]["inputs"] == [
        "chatgpt_chats/ingest"
    ]
    assert steps[("chatgpt_chats", "keyword_index")]["inputs"] == [
        "chatgpt_chats/render_markdown"
    ]


def test_adding_sources_to_an_empty_root_checks_the_config_then_turns_their_search_off(
    workspace: _SyncWorkspace,
) -> None:
    datalib = _FakeDatalib()

    added = import_chats.ensure_sources(workspace.datalib, [CLAUDE, CHATGPT], datalib)

    assert added == [CLAUDE, CHATGPT]
    config_path = workspace.datalib.config_path
    check, *_ = datalib.commands
    assert check[1:] == [
        "--check",
        str(config_path.with_name("config.toml.import-chats")),
    ]
    assert datalib.turned_off == [
        "claude_chats/render_markdown",
        "claude_chats/keyword_index",
        "chatgpt_chats/render_markdown",
        "chatgpt_chats/keyword_index",
    ]
    assert {c[-1] for c in datalib.commands if c[1] == "turn-off"} == {"import-chats"}
    assert [
        group["id"] for group in tomllib.loads(config_path.read_text())["groups"]
    ] == [
        "claude_chats",
        "chatgpt_chats",
    ]
    assert sorted(path.name for path in config_path.parent.iterdir()) == ["config.toml"]


def test_adding_a_source_keeps_the_config_as_it_was_and_leaves_a_source_already_there_alone(
    workspace: _SyncWorkspace,
) -> None:
    config_path = workspace.datalib.config_path
    config_path.parent.mkdir(parents=True)
    existing = (
        "# the user's own Slack\n"
        '[[groups]]\nid = "slack"\ntype = "slack"\n\n'
        '[[steps]]\ngroup = "slack"\nfunction = "ingest"\n[steps.params.api]\n\n'
        '[[groups]]\nid = "claude_chats"\ntype = "claude"\n'
    )
    config_path.write_text(existing)
    datalib = _FakeDatalib()

    added = import_chats.ensure_sources(workspace.datalib, [CLAUDE, CHATGPT], datalib)

    assert added == [CHATGPT]
    text = config_path.read_text()
    assert text.startswith(existing.rstrip("\n") + "\n\n# ChatGPT conversations")
    assert datalib.turned_off == [
        "chatgpt_chats/render_markdown",
        "chatgpt_chats/keyword_index",
    ]


def test_adding_sources_already_configured_runs_nothing(
    workspace: _SyncWorkspace,
) -> None:
    datalib = _FakeDatalib()
    import_chats.ensure_sources(workspace.datalib, [CLAUDE], datalib)
    before = workspace.datalib.config_path.read_text()
    datalib.commands.clear()

    assert import_chats.ensure_sources(workspace.datalib, [CLAUDE], datalib) == []
    assert datalib.commands == []
    assert workspace.datalib.config_path.read_text() == before


def test_a_group_of_another_type_under_a_sources_id_is_refused(
    workspace: _SyncWorkspace,
) -> None:
    config_path = workspace.datalib.config_path
    config_path.parent.mkdir(parents=True)
    config_path.write_text('[[groups]]\nid = "chatgpt_chats"\ntype = "slack"\n')

    with pytest.raises(import_chats.ImportChatsError, match="of type 'slack'"):
        import_chats.ensure_sources(workspace.datalib, [CHATGPT], _FakeDatalib())

    assert (
        config_path.read_text() == '[[groups]]\nid = "chatgpt_chats"\ntype = "slack"\n'
    )


def test_a_config_that_is_not_toml_is_refused(workspace: _SyncWorkspace) -> None:
    config_path = workspace.datalib.config_path
    config_path.parent.mkdir(parents=True)
    config_path.write_text("[[groups]\n")

    with pytest.raises(import_chats.ImportChatsError, match="is not valid TOML"):
        import_chats.ensure_sources(workspace.datalib, [CLAUDE], _FakeDatalib())


def test_a_config_datalib_refuses_is_never_put_in_place(
    workspace: _SyncWorkspace,
) -> None:
    config_path = workspace.datalib.config_path
    config_path.parent.mkdir(parents=True)
    config_path.write_text("# mine\n")
    datalib = _FakeDatalib(check_returncode=2)

    with pytest.raises(import_chats.ImportChatsError, match="refused the config"):
        import_chats.ensure_sources(workspace.datalib, [CLAUDE], datalib)

    assert config_path.read_text() == "# mine\n"
    assert datalib.turned_off == []
    assert sorted(path.name for path in config_path.parent.iterdir()) == ["config.toml"]


@pytest.mark.parametrize(
    "previous", [None, "# mine\n"], ids=["no-config-before", "a-config-before"]
)
def test_a_search_step_that_cannot_be_turned_off_puts_the_config_back(
    workspace: _SyncWorkspace, previous: str | None
) -> None:
    config_path = workspace.datalib.config_path
    if previous is not None:
        config_path.parent.mkdir(parents=True)
        config_path.write_text(previous)
    datalib = _FakeDatalib(turn_off_failures=["claude_chats/keyword_index"])

    with pytest.raises(
        import_chats.ImportChatsError,
        match="could not turn off claude_chats/keyword_index",
    ):
        import_chats.ensure_sources(workspace.datalib, [CLAUDE], datalib)

    assert (config_path.read_text() if config_path.exists() else None) == previous


# --- datalib's and latchkey's answers --------------------------------------------------------------------------------


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


def test_a_check_without_latchkey_says_so() -> None:
    def run(command: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess:
        raise FileNotFoundError(2, "No such file or directory", command[0])

    with pytest.raises(import_chats.ImportChatsError, match="cannot run latchkey"):
        import_chats.check(CLAUDE, run)


# --- the raw stores --------------------------------------------------------------------------------------------------


def test_the_raw_store_counts_only_conversations_whose_detail_arrived(
    workspace: _SyncWorkspace,
) -> None:
    _raw_store(
        workspace.data_root,
        CLAUDE,
        [("a", "A", None), ("b", "B", None)],
        projects=[("p", "Project", None)],
        unfetched=["c"],
    )

    assert (
        import_chats.count_conversations(workspace.datalib, CLAUDE, _FakeDatalib()) == 2
    )


def test_a_source_with_no_raw_store_has_no_count_and_none_is_created(
    workspace: _SyncWorkspace,
) -> None:
    datalib = _FakeDatalib()

    assert import_chats.count_conversations(workspace.datalib, CHATGPT, datalib) is None
    assert datalib.commands == []
    assert not workspace.datalib.raw_store(CHATGPT).exists()


def test_a_query_that_matches_nothing_reads_as_no_rows(
    workspace: _SyncWorkspace,
) -> None:
    _raw_store(workspace.data_root, CHATGPT)

    assert (
        import_chats.query_raw_store(
            workspace.datalib, CHATGPT, CHATGPT.conversations_query, _FakeDatalib()
        )
        == []
    )
    assert (
        import_chats.count_conversations(workspace.datalib, CHATGPT, _FakeDatalib())
        == 0
    )


def test_a_raw_store_that_cannot_be_read_has_no_count(
    workspace: _SyncWorkspace,
) -> None:
    _raw_store(workspace.data_root, CHATGPT)

    def run(command: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            command, 1, "", "Error: database is locked\n"
        )

    assert import_chats.count_conversations(workspace.datalib, CHATGPT, run) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-10-07T08:00:00.000000+00:00", "2026-10-07T08:00:00+00:00"),
        ("2026-10-07T08:00:00", "2026-10-07T08:00:00+00:00"),
        ("1759824000.25", "2025-10-07T08:00:00.250000+00:00"),
        ("1759824000", "2025-10-07T08:00:00+00:00"),
        ('"2026-10-07T08:00:00Z"', "2026-10-07T08:00:00+00:00"),
    ],
)
def test_a_raw_stores_timestamp_is_read_as_iso_text_or_a_json_epoch(
    raw: str, expected: str
) -> None:
    parsed = import_chats.parse_updated_at(raw)

    assert parsed is not None
    assert parsed.isoformat() == expected


@pytest.mark.parametrize("raw", [None, "", "null", "true", "not a time", "1e400"])
def test_a_raw_stores_timestamp_that_is_none_of_those_is_no_date(
    raw: str | None,
) -> None:
    assert import_chats.parse_updated_at(raw) is None


# --- the index -------------------------------------------------------------------------------------------------------


def _rendered_page(data_root: Path, group: str, uuid: str, external_id: str) -> Path:
    """A page laid out as datalib's chat renderer writes one, as far as the index reads it: its frontmatter."""
    page = data_root / group / "render_markdown" / uuid / "all.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(
        "---\n"
        '"title": "x"\n'
        f"chat_uuid: {uuid}\n"
        f"external_id: {json.dumps(external_id)}\n"
        "item_count: 2\n"
        "---\n\n"
        "# x\n\nexternal_id: not-frontmatter\n"
    )
    return page


def test_the_index_lists_chats_by_month_newest_first_then_undated_then_projects(
    workspace: _SyncWorkspace,
) -> None:
    _raw_store(
        workspace.data_root,
        CLAUDE,
        [
            ("a", "Older [draft] <T> in C:\\", "2026-09-03T08:00:00.000000+00:00"),
            ("b", "Newest", "2026-10-07T08:00:00.000000+00:00"),
            ("c", "Middle", "2026-10-01T08:00:00.000000+00:00"),
            ("d", "No dates", None),
            ("e", None, "2026-10-02T08:00:00.000000+00:00"),
        ],
        projects=[("p", "Stellar Cartography", "2026-08-01T00:00:00+00:00")],
        unfetched=["f"],
    )
    index_path = import_chats.index_path_for(workspace.status_path, CLAUDE)

    import_chats.write_index(workspace.datalib, CLAUDE, index_path, _FakeDatalib())

    lines = index_path.read_text().splitlines()
    assert lines[0] == "# Claude chats"
    assert lines[2].startswith("5 conversations, most recently updated first.")
    assert [line for line in lines if line.startswith("## ")] == [
        "## October 2026",
        "## September 2026",
        "## Undated",
        "## Projects",
    ]
    assert [line for line in lines if line.startswith("- ")] == [
        "- 2026-10-07 · [Newest](https://claude.ai/chat/b)",
        "- 2026-10-02 · [(untitled)](https://claude.ai/chat/e)",
        "- 2026-10-01 · [Middle](https://claude.ai/chat/c)",
        "- 2026-09-03 · [Older \\[draft\\] \\<T> in C:\\\\](https://claude.ai/chat/a)",
        "- [No dates](https://claude.ai/chat/d)",
        "- 2026-08-01 · [Stellar Cartography](https://claude.ai/project/p)",
    ]
    assert list(index_path.parent.iterdir()) == [index_path]


def test_once_search_is_on_the_index_links_each_rendered_page(
    workspace: _SyncWorkspace,
) -> None:
    _raw_store(
        workspace.data_root,
        CHATGPT,
        [
            ("c-1", "Rendered", "1759824000.5"),
            ("c-2", "Not yet rendered", "1759737600.0"),
        ],
    )
    _rendered_page(workspace.data_root, "chatgpt_chats", "uuid 1", "c-1")
    index_path = import_chats.index_path_for(workspace.status_path, CHATGPT)

    import_chats.write_index(workspace.datalib, CHATGPT, index_path, _FakeDatalib())

    assert [
        line for line in index_path.read_text().splitlines() if line.startswith("- ")
    ] == [
        "- 2025-10-07 · [Rendered](https://chatgpt.com/c/c-1) · [page](../datalib/chatgpt_chats/render_markdown/uuid%201/all.md)",
        "- 2025-10-06 · [Not yet rendered](https://chatgpt.com/c/c-2)",
    ]


def test_an_index_of_a_source_with_nothing_downloaded_says_so(
    workspace: _SyncWorkspace,
) -> None:
    _raw_store(workspace.data_root, CHATGPT)
    index_path = import_chats.index_path_for(workspace.status_path, CHATGPT)

    import_chats.write_index(workspace.datalib, CHATGPT, index_path, _FakeDatalib())

    assert (
        index_path.read_text()
        .splitlines()[2]
        .startswith("0 conversations, most recently updated first.")
    )


def test_an_index_whose_raw_store_cannot_be_read_is_left_as_it_was(
    workspace: _SyncWorkspace,
) -> None:
    index_path = import_chats.index_path_for(workspace.status_path, CHATGPT)

    with pytest.raises(import_chats.ImportChatsError, match="could not be read"):
        import_chats.write_index(workspace.datalib, CHATGPT, index_path, _FakeDatalib())

    assert not index_path.exists()


def test_a_long_title_with_tabs_is_one_line_cut_short_in_the_index(
    workspace: _SyncWorkspace,
) -> None:
    _raw_store(workspace.data_root, CHATGPT, [("x", "word\t  " * 100, "1759824000")])
    index_path = import_chats.index_path_for(workspace.status_path, CHATGPT)

    import_chats.write_index(workspace.datalib, CHATGPT, index_path, _FakeDatalib())

    (entry,) = [
        line for line in index_path.read_text().splitlines() if line.startswith("- ")
    ]
    title = entry.split("[", 1)[1].split("](", 1)[0]
    assert len(title) == 120
    assert title.startswith("word word ")
    assert title.endswith("word…")
    assert entry.endswith("(https://chatgpt.com/c/x)")


# --- a sync ----------------------------------------------------------------------------------------------------------


def test_a_sync_installs_adds_the_sources_runs_their_downloads_and_records_what_it_imported(
    workspace: _SyncWorkspace,
) -> None:
    def ingest() -> None:
        _raw_store(
            workspace.data_root,
            CLAUDE,
            [("a", "A", None), ("b", "B", None), ("c", "C", None)],
        )
        _raw_store(workspace.data_root, CHATGPT, [("d", "D", None), ("e", "E", None)])

    datalib = _FakeDatalib(
        stderr=_run_summary(
            {"step": "claude_chats/ingest", "status": "succeeded"},
            {"step": "chatgpt_chats/ingest", "status": "succeeded"},
        ),
        on_sync=ingest,
    )

    assert workspace.sync([CLAUDE, CHATGPT], datalib) is True

    install = datalib.commands[0]
    assert install == ["bash", "install_datalib.sh", "--without-app"]
    assert len(datalib.turned_off) == 4
    (dag,) = datalib.syncs
    assert dag == [
        str(workspace.datalib.binary("datalib-dag")),
        str(workspace.datalib.config_path),
        "--sync",
        "claude_chats/ingest,chatgpt_chats/ingest",
        "--by",
        "import-chats",
    ]
    assert datalib.commands.index(dag) > datalib.commands.index(
        next(c for c in datalib.commands if c[1:2] == ["turn-off"])
    )
    sources = import_chats.read_status(workspace.status_path)["sources"]
    assert {
        key: (record["state"], record["conversations"], record["pid"])
        for key, record in sources.items()
    } == {
        "claude": ("imported", 3, None),
        "chatgpt": ("imported", 2, None),
    }
    index_dir = workspace.status_path.parent
    assert "3 conversations" in (index_dir / "claude-chats.md").read_text()
    assert "2 conversations" in (index_dir / "chatgpt-chats.md").read_text()


def test_a_later_sync_of_one_source_leaves_the_other_in_the_config(
    workspace: _SyncWorkspace,
) -> None:
    summary = _run_summary({"step": "chatgpt_chats/ingest", "status": "succeeded"})
    workspace.sync([CLAUDE, CHATGPT], _FakeDatalib(stderr=summary))
    datalib = _FakeDatalib(stderr=summary)

    workspace.sync([CHATGPT], datalib)

    assert datalib.turned_off == []
    groups = tomllib.loads(workspace.datalib.config_path.read_text())["groups"]
    assert [group["id"] for group in groups] == ["claude_chats", "chatgpt_chats"]


def test_a_sync_records_a_sign_in_problem_for_one_source_and_success_for_the_other(
    workspace: _SyncWorkspace,
) -> None:
    datalib = _FakeDatalib(
        stderr=_run_summary(
            {"step": "claude_chats/ingest", "status": "succeeded"},
            {
                "step": "chatgpt_chats/ingest",
                "status": "failed",
                "error": "step exited 1\ncaused by: chatgpt: HTTP 401 token_expired\n",
            },
        ),
        on_sync=lambda: _raw_store(workspace.data_root, CLAUDE, [("a", "A", None)]),
    )

    assert workspace.sync([CLAUDE, CHATGPT], datalib) is False

    assert workspace.record("claude")["state"] == "imported"
    chatgpt = workspace.record("chatgpt")
    assert (chatgpt["state"], chatgpt["detail"], chatgpt["conversations"]) == (
        "needs_sign_in",
        "chatgpt: HTTP 401 token_expired",
        0,
    )


def test_a_download_that_gave_up_partway_is_imported_with_what_it_got(
    workspace: _SyncWorkspace,
) -> None:
    datalib = _FakeDatalib(
        stderr=_run_summary(
            {
                "step": "chatgpt_chats/ingest",
                "status": "failed",
                "failure": "incomplete",
                "error": "step exited 1\ncaused by: gave up: 40 conversations still owed after HTTP 429\n",
                "outputs": [{"path": "chatgpt_chats/ingest", "changed": True}],
            }
        ),
        on_sync=lambda: _raw_store(
            workspace.data_root, CHATGPT, [("a", "A", None), ("b", "B", None)]
        ),
    )

    assert workspace.sync([CHATGPT], datalib) is True

    record = workspace.record("chatgpt")
    assert (record["state"], record["conversations"]) == ("imported", 2)
    assert "40 conversations still owed after HTTP 429" in record["detail"]
    assert "an update fetches the rest" in record["detail"]


def test_a_search_step_that_failed_once_turned_on_leaves_the_source_imported_and_says_so(
    workspace: _SyncWorkspace,
) -> None:
    datalib = _FakeDatalib(
        stderr=_run_summary(
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
    )

    assert workspace.sync([CLAUDE, CHATGPT], datalib) is True

    claude = workspace.record("claude")
    assert (claude["state"], claude["detail"]) == (
        "imported",
        "search could not be updated: render store: disk full",
    )
    assert workspace.record("chatgpt") | {"updated_at": None} == {
        "state": "imported",
        "conversations": 0,
        "updated_at": None,
        "detail": "",
        "pid": None,
        "fetched": None,
        "to_fetch": None,
    }


def test_a_sync_that_never_summarised_records_the_failure_and_its_output(
    workspace: _SyncWorkspace,
) -> None:
    datalib = _FakeDatalib(
        stderr='{"event":"run_plan","steps":[]}\nError: the run store is locked\n',
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
        stderr="datalib-dag: the loop on datalib is already running (pid 7); following request r1 there\n",
        stdout="request r1: done\n",
        returncode=0,
        on_sync=lambda: _raw_store(
            workspace.data_root, CLAUDE, [("a", "A", None), ("b", "B", None)]
        ),
    )

    assert workspace.sync([CLAUDE], datalib) is True

    record = workspace.record("claude")
    assert (record["state"], record["conversations"]) == ("imported", 2)


def test_a_sync_marks_its_sources_importing_with_its_pid_while_it_runs(
    workspace: _SyncWorkspace,
) -> None:
    seen: list[dict] = []
    datalib = _FakeDatalib(
        stderr=_run_summary({"step": "claude_chats/ingest", "status": "succeeded"}),
        on_sync=lambda: seen.append(workspace.record("claude")),
    )

    workspace.sync([CLAUDE], datalib)

    assert [(record["state"], record["pid"]) for record in seen] == [
        ("importing", os.getpid())
    ]


def _installer_fails(
    command: Sequence[str], **_kwargs: object
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        command, 1, "[install_datalib] installing\n", "curl: (6) no network\n"
    )


def _installer_hangs(
    command: Sequence[str], timeout: float | None = None, **_kwargs: object
) -> subprocess.CompletedProcess:
    assert timeout is not None
    raise subprocess.TimeoutExpired(command, timeout)


def _nothing_can_start(
    command: Sequence[str], **_kwargs: object
) -> subprocess.CompletedProcess:
    raise PermissionError(13, "Permission denied", command[0])


def _datalib_dag_cannot_start(
    command: Sequence[str], **kwargs: object
) -> subprocess.CompletedProcess:
    if Path(command[0]).name == "datalib-dag":
        raise PermissionError(13, "Permission denied", command[0])
    return _FakeDatalib()(command, **kwargs)


@pytest.mark.parametrize(
    ("run", "reason"),
    [
        (_installer_fails, "could not be installed: curl: (6) no network"),
        (_installer_hangs, "did not install within"),
        (
            _nothing_can_start,
            "cannot run the datalib installer: [Errno 13] Permission denied",
        ),
        (_datalib_dag_cannot_start, "cannot run datalib-dag"),
    ],
    ids=[
        "installer-fails",
        "installer-hangs",
        "nothing-can-start",
        "datalib-dag-cannot-start",
    ],
)
def test_a_sync_that_cannot_run_records_the_failure_and_reports_it_as_an_import_error(
    workspace: _SyncWorkspace,
    run: Callable[..., subprocess.CompletedProcess],
    reason: str,
) -> None:
    with pytest.raises(import_chats.ImportChatsError) as raised:
        workspace.sync([CLAUDE], run)

    assert reason in str(raised.value)
    record = workspace.record("claude")
    assert record["state"] == "failed"
    assert reason in record["detail"]


def test_a_sync_whose_index_cannot_be_written_still_records_what_it_imported(
    workspace: _SyncWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    datalib = _FakeDatalib(
        stderr=_run_summary({"step": "claude_chats/ingest", "status": "succeeded"}),
        on_sync=lambda: _raw_store(
            workspace.data_root, CLAUDE, [("a", "A", None), ("b", "B", None)]
        ),
    )
    index_path = import_chats.index_path_for(workspace.status_path, CLAUDE)
    index_path.mkdir(parents=True)

    assert workspace.sync([CLAUDE], datalib) is True

    record = workspace.record("claude")
    assert (record["state"], record["conversations"]) == ("imported", 2)
    assert f"their index {index_path} could not be rewritten" in capsys.readouterr().err
    assert not list(index_path.parent.glob("*.tmp-*"))


def test_a_sync_keeps_the_last_count_when_the_raw_store_cannot_be_read(
    workspace: _SyncWorkspace,
) -> None:
    import_chats.record_source(
        workspace.status_path, "claude", "imported", 7, "", pid=None
    )

    workspace.sync(
        [CLAUDE],
        _FakeDatalib(
            stderr=_run_summary({"step": "claude_chats/ingest", "status": "succeeded"})
        ),
    )

    assert workspace.record("claude")["conversations"] == 7


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


# --- progress --------------------------------------------------------------------------------------------------------


def test_a_running_sync_records_the_conversations_downloaded_so_far(
    workspace: _SyncWorkspace,
) -> None:
    seen_counts: list[int] = []

    def ingest() -> None:
        seen_counts.append(workspace.record("chatgpt")["conversations"])
        _raw_store(
            workspace.data_root,
            CHATGPT,
            [("a", "A", None), ("b", "B", None), ("c", "C", None)],
        )
        seen_counts.extend(
            record["conversations"]
            for record in workspace.watch_record(
                "chatgpt",
                lambda record: record["state"] == "importing"
                and record["conversations"] == 3,
            )
        )

    datalib = _FakeDatalib(
        stderr=_run_summary({"step": "chatgpt_chats/ingest", "status": "succeeded"}),
        on_sync=ingest,
    )

    workspace.sync([CHATGPT], datalib, progress_interval_seconds=0.01)

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
        [CHATGPT], _FakeDatalib(), stream=stream, progress_interval_seconds=0.01
    )

    assert seen[-1] == (2, 5)
    final = workspace.record("chatgpt")
    assert (final["state"], final["fetched"], final["to_fetch"]) == (
        "imported",
        None,
        None,
    )


def test_a_sync_keeps_its_record_fresh_while_datalib_installs(
    workspace: _SyncWorkspace,
) -> None:
    rewritten_during_install: list[bool] = []
    fake = _FakeDatalib(
        stderr=_run_summary({"step": "chatgpt_chats/ingest", "status": "succeeded"})
    )

    def run(command: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess:
        if command[0] == "bash":
            written_at_start = workspace.status_path.stat().st_mtime_ns
            rewritten_during_install.append(
                _wait_until(
                    lambda: workspace.status_path.stat().st_mtime_ns != written_at_start
                )
            )
        return fake(command, **kwargs)

    workspace.sync([CHATGPT], run, progress_interval_seconds=0.01)

    assert rewritten_during_install == [True]


def test_a_running_sync_keeps_recording_after_a_record_it_could_not_write(
    workspace: _SyncWorkspace, capsys: pytest.CaptureFixture[str]
) -> None:
    warnings: list[str] = []
    rerecorded: list[bool] = []

    def is_warned() -> bool:
        warnings.append(capsys.readouterr().err)
        return "could not record the ChatGPT import's progress" in "".join(warnings)

    def is_recorded_again() -> bool:
        record = import_chats.read_status(workspace.status_path)["sources"].get(
            "chatgpt"
        )
        return record is not None and record["state"] == "importing"

    def ingest() -> None:
        workspace.status_path.write_text("{not json")
        _wait_until(is_warned)
        import_chats.write_status(workspace.status_path, {"sources": {}})
        rerecorded.append(_wait_until(is_recorded_again))

    datalib = _FakeDatalib(
        stderr=_run_summary({"step": "chatgpt_chats/ingest", "status": "succeeded"}),
        on_sync=ingest,
    )

    assert workspace.sync([CHATGPT], datalib, progress_interval_seconds=0.01) is True

    assert "could not record the ChatGPT import's progress" in "".join(warnings)
    assert rerecorded == [True]
    assert workspace.record("chatgpt")["state"] == "imported"

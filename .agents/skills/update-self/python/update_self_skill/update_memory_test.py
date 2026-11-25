"""What update-self reads off the workspace tree's memory tool, held to each release's output.

The staged skill runs in a workspace as old as the oldest release the app updates from, and asks
that workspace's own tool for its listing. The tool first shipped in ``minds-v0.8.2`` as
``memory_candidates.py``, and its JSON grew release by release; the documents below are its
``report_to_json`` at the releases where it changed. The tool is a fake script at the real path,
so the tests exercise the real shell-out.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from update_self_skill import update_memory, update_self

_AGENT = {
    "name": "weekly-report",
    "id": "agent-0123456789abcdef0123456789abcdef",
    "display_name": "Weekly report",
    "kind": "chat",
    "state": "WAITING",
    "last_activity": "2026-09-24T09:00:00Z",
    "idle_seconds": 10800,
    "pids": [40],
}
_APP = {
    "name": "notes",
    "display_name": "Notes",
    "program": "notes-app",
    "window_count": 2,
    "stop_when_no_windows": False,
    "pids": [50],
}
_BROWSER = {
    "name": "browser-1",
    "controller": "free",
    "tab_count": 3,
    "active_url": "https://example.com/",
    "pids": [60],
}
_FREE = {
    "field": "MemAvailable",
    "free_kib": 2 * 1024 * 1024,
    "total_kib": 8 * 1024 * 1024,
    "source": "/mngr-vol/.host-meminfo",
}


def _listing_at_0_8_2() -> dict[str, Any]:
    """No apps section, and sizes are resident sizes (``rss_kib``)."""
    return {
        "generated_at": "2026-09-24T12:00:00Z",
        "idle_after_seconds": 900,
        "free_memory": {**_FREE, "source": "/proc/meminfo"},
        "agents": {"candidates": [{**_AGENT, "rss_kib": 600_000}], "notes": []},
        "browsers": {"candidates": [{**_BROWSER, "rss_kib": 900_000}], "notes": []},
    }


def _listing_at_0_9_2() -> dict[str, Any]:
    """Apps are listed; sizes are still resident sizes."""
    return {
        **_listing_at_0_8_2(),
        "free_memory": dict(_FREE),
        "apps": {"candidates": [{**_APP, "rss_kib": 300_000}], "notes": []},
    }


def _listing_at_0_9_5() -> dict[str, Any]:
    """Sizes are anonymous memory (``anonymous_kib``)."""
    return {
        "generated_at": "2026-09-24T12:00:00Z",
        "idle_after_seconds": 900,
        "free_memory": dict(_FREE),
        "agents": {"candidates": [{**_AGENT, "anonymous_kib": 200_000}], "notes": []},
        "apps": {"candidates": [{**_APP, "anonymous_kib": 100_000}], "notes": []},
        "browsers": {
            "candidates": [{**_BROWSER, "anonymous_kib": 400_000}],
            "notes": [],
        },
    }


def _listing_at_0_10_7() -> dict[str, Any]:
    """The free memory says how much is left before the memory guard acts."""
    listing = _listing_at_0_9_5()
    listing["free_memory"] = {
        **_FREE,
        "shed_line_kib": 838_860,
        "room_kib": 1_258_292,
    }
    return listing


def _write_tool(repo_root: Path, tool: Path, body: str) -> None:
    script = repo_root / tool
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(body)


def _write_listing_tool(repo_root: Path, tool: Path, listing: dict[str, Any]) -> None:
    """A tool that prints ``listing`` when asked for JSON the way its release takes the request,
    and fails on any other arguments."""
    expected = (
        ["list", "--json"] if tool == update_memory.WORKSPACE_MEMORY_REL else ["--json"]
    )
    _write_tool(
        repo_root,
        tool,
        "import json, sys\n"
        f"if sys.argv[1:] != {expected!r}:\n"
        "    sys.exit(f'unexpected arguments: {sys.argv[1:]}')\n"
        f"print(json.dumps({listing!r}))\n",
    )


@pytest.mark.parametrize(
    ("listing", "room_kib", "sizes", "is_resident_size", "lists_apps"),
    [
        (_listing_at_0_8_2(), None, (600_000, None, 900_000), True, False),
        (_listing_at_0_9_2(), None, (600_000, 300_000, 900_000), True, True),
        (_listing_at_0_9_5(), None, (200_000, 100_000, 400_000), False, True),
        (_listing_at_0_10_7(), 1_258_292, (200_000, 100_000, 400_000), False, True),
    ],
    ids=["0.8.2", "0.9.2", "0.9.5", "0.10.7"],
)
def test_reads_each_releases_listing_and_reports_what_it_lacks_as_unknown(
    tmp_path: Path,
    listing: dict[str, Any],
    room_kib: int | None,
    sizes: tuple[int, int | None, int],
    is_resident_size: bool,
    lists_apps: bool,
) -> None:
    _write_listing_tool(tmp_path, update_memory.MEMORY_CANDIDATES_REL, listing)

    reading = update_memory.read_memory(tmp_path)

    agent_kib, app_kib, browser_kib = sizes
    assert reading.tool == update_memory.MEMORY_CANDIDATES_REL
    assert reading.free is not None
    assert (reading.free.free_kib, reading.free.room_kib) == (2 * 1024 * 1024, room_kib)
    assert reading.agents == (
        update_memory.AgentRow(
            kind="chat",
            name="weekly-report",
            title="Weekly report",
            idle_seconds=10800.0,
            memory_kib=agent_kib,
        ),
    )
    assert reading.browsers == (
        update_memory.BrowserRow(name="browser-1", memory_kib=browser_kib),
    )
    assert reading.is_resident_size is is_resident_size
    if lists_apps:
        assert reading.apps == (
            update_memory.AppRow(
                program="notes-app",
                title="Notes",
                window_count=2,
                stops_when_no_windows=False,
                memory_kib=app_kib,
            ),
        )
        assert reading.unknown == ()
    else:
        assert reading.apps is None
        assert reading.unknown == (
            "apps: this workspace's version does not list them",
        )


def test_prefers_the_current_tool_and_asks_it_the_way_it_takes_the_request(
    tmp_path: Path,
) -> None:
    _write_listing_tool(
        tmp_path, update_memory.WORKSPACE_MEMORY_REL, _listing_at_0_10_7()
    )
    _write_tool(
        tmp_path,
        update_memory.MEMORY_CANDIDATES_REL,
        "import sys\nsys.exit('the earlier tool must not run beside the current one')\n",
    )

    reading = update_memory.read_memory(tmp_path)

    assert reading.tool == update_memory.WORKSPACE_MEMORY_REL
    assert reading.unknown == ()
    assert reading.apps is not None and reading.apps[0].program == "notes-app"


def test_a_workspace_with_no_memory_tool_says_only_chats_can_be_stopped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert update_self.main(["memory", "--repo-root", str(tmp_path)]) == 0

    printed = capsys.readouterr().out
    assert "Free memory: unknown" in printed
    assert "run-status stop-chats --agent" in printed
    reading = update_memory.read_memory(tmp_path)
    assert (reading.tool, reading.agents, reading.apps, reading.browsers) == (
        None,
        None,
        None,
        None,
    )


def test_a_source_the_tool_could_not_read_is_unknown_with_the_tools_own_reason(
    tmp_path: Path,
) -> None:
    listing = _listing_at_0_10_7()
    listing["apps"] = {
        "candidates": None,
        "notes": ["could not read the shell's apps at http://127.0.0.1:8000"],
    }
    listing["free_memory"] = None
    _write_listing_tool(tmp_path, update_memory.WORKSPACE_MEMORY_REL, listing)

    reading = update_memory.read_memory(tmp_path)

    assert reading.apps is None
    assert reading.agents is not None and reading.browsers is not None
    assert reading.unknown == (
        "apps: could not read the shell's apps at http://127.0.0.1:8000",
        "free memory: the tool could not read it",
    )


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ("import sys\nsys.exit('mngr is not installed')\n", "exited 1: mngr is not installed"),
        ("print('Free memory: 12 MiB')\n", "did not print JSON"),
        ("print('[]')\n", "printed JSON that is not a listing"),
    ],
    ids=["fails", "prints text", "prints another shape"],
)
def test_a_tool_that_fails_or_prints_something_else_leaves_everything_unknown(
    tmp_path: Path, body: str, reason: str
) -> None:
    _write_tool(tmp_path, update_memory.WORKSPACE_MEMORY_REL, body)

    reading = update_memory.read_memory(tmp_path)

    assert (reading.free, reading.agents, reading.apps, reading.browsers) == (
        None,
        None,
        None,
        None,
    )
    assert len(reading.unknown) == 1 and reason in reading.unknown[0]


def test_the_command_prints_what_each_stop_takes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_listing_tool(
        tmp_path, update_memory.WORKSPACE_MEMORY_REL, _listing_at_0_10_7()
    )

    assert update_self.main(["memory", "--repo-root", str(tmp_path)]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == (
        "Free memory: 2048 MiB (MemAvailable from /mngr-vol/.host-meminfo); "
        "1228 MiB of it before the memory guard starts stopping processes"
    )
    rows = [line.split() for line in lines]
    assert ["chat", "weekly-report", "Weekly", "report", "3h", "00m", "195", "MiB"] in rows
    assert ["notes-app", "Notes", "2", "no", "97", "MiB"] in rows
    assert ["browser-1", "390", "MiB"] in rows


def test_resident_sizes_are_called_out_as_overstating_what_a_stop_frees(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_listing_tool(
        tmp_path, update_memory.MEMORY_CANDIDATES_REL, _listing_at_0_9_2()
    )

    assert update_self.main(["memory", "--repo-root", str(tmp_path)]) == 0

    printed = capsys.readouterr().out
    assert "resident sizes" in printed
    assert "left before the memory guard starts stopping processes is unknown" in printed


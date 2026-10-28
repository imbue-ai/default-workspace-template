"""Tests for the process reader and the records of who runs what: chats, user apps, and the agent registry."""

import json
from pathlib import Path

import pytest

from system_monitor import programs


def _write_proc_entry(
    proc: Path, pid: int, ppid: int, rss_kib: int, argv: list[str]
) -> None:
    entry = proc / str(pid)
    entry.mkdir(parents=True)
    (entry / "status").write_text(f"Name:\tx\nPPid:\t{ppid}\nVmRSS:\t{rss_kib} kB\n")
    (entry / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in argv) + b"\0")


def test_processes_are_read_from_numeric_entries_and_unreadable_ones_are_skipped(
    tmp_path: Path,
) -> None:
    _write_proc_entry(tmp_path, 42, 1, 2048, ["python3", "-m", "thing"])
    (tmp_path / "self").mkdir()
    (tmp_path / "43").mkdir()

    procs = programs.read_processes(tmp_path)

    assert list(procs) == [42]
    assert (procs[42].ppid, procs[42].rss, procs[42].argv) == (
        1,
        2048 * 1024,
        ["python3", "-m", "thing"],
    )
    # No smaps_rollup (a kernel without one, or a process not ours to read): no breakdown.
    assert (procs[42].private, procs[42].pss) == (None, None)
    assert not programs.has_breakdown(procs)


def test_private_and_proportional_memory_are_read_from_smaps_rollup(
    tmp_path: Path,
) -> None:
    _write_proc_entry(tmp_path, 42, 1, 33236, ["system-monitor"])
    (tmp_path / "42" / "smaps_rollup").write_text(
        "00400000-ffffc2f5b000 ---p 00000000 00:00 0                              [rollup]\n"
        "Rss:               33236 kB\n"
        "Pss:               22440 kB\n"
        "Shared_Clean:      11496 kB\n"
        "Private_Clean:       100 kB\n"
        "Private_Dirty:     21640 kB\n"
    )

    procs = programs.read_processes(tmp_path)

    assert (procs[42].private, procs[42].pss) == (21740 * 1024, 22440 * 1024)
    assert programs.has_breakdown(procs)


def test_chat_names_prefer_the_display_label_and_drop_the_agent_suffix(
    tmp_path: Path,
) -> None:
    for agent_id, data in (
        ("agent-a", {"labels": {"display_name": "Budget Agent"}, "name": "ignored"}),
        ("agent-b", {"name": "notes-Agent"}),
        ("agent-c", {}),
    ):
        (tmp_path / agent_id).mkdir()
        (tmp_path / agent_id / "data.json").write_text(json.dumps(data))
    (tmp_path / "agent-d").mkdir()
    (tmp_path / "agent-d" / "data.json").write_text("not json")

    chats = programs.chats(tmp_path)

    assert {agent_id: chat.name for agent_id, chat in chats.items()} == {
        "agent-a": "Budget",
        "agent-b": "notes",
        "agent-c": "agent-c",
    }
    assert programs.chats(tmp_path / "missing") == {}


def test_user_apps_lists_only_the_user_band_rows_that_name_a_program(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "apps.toml"
    registry.write_text(
        '[[apps]]\nname = "chat"\nprogram = "chat"\npriority = "chat"\n\n'
        '[[apps]]\nname = "budget"\ndisplay_name = "Budget"\nprogram = "budget"\npriority = "user"\n\n'
        '[[apps]]\nname = "notes"\nprogram = "notes"\npriority = "user"\n\n'
        '[[apps]]\nname = "orphan"\npriority = "user"\n'
    )

    assert programs.user_apps(registry) == {"budget": "Budget", "notes": "notes"}
    assert programs.user_apps(tmp_path / "missing.toml") == {}


def test_malformed_workspace_records_are_skipped_rather_than_failing(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "apps.toml"
    registry.write_text(
        '[[apps]]\nprogram = "nameless"\npriority = "user"\n\n'
        '[[apps]]\nname = "budget"\nprogram = "budget"\npriority = "user"\n'
    )
    assert programs.user_apps(registry) == {"nameless": "nameless", "budget": "budget"}
    registry.write_text('apps = "not a list"\n')
    assert programs.user_apps(registry) == {}

    agents = tmp_path / "agents"
    for agent_id, body in (
        ("agent-list", "[1, 2]"),
        ("agent-labels", json.dumps({"labels": "oops", "name": "notes"})),
    ):
        (agents / agent_id).mkdir(parents=True)
        (agents / agent_id / "data.json").write_text(body)
    assert programs.chats(agents) == {
        "agent-labels": programs.Chat("notes", "agent-labels")
    }


def test_chats_on_any_harness_are_found_through_the_agent_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OOM_PRIORITY_RUNTIME_DIR", str(tmp_path))
    registry = tmp_path / "agent_pids"
    registry.mkdir()
    # A codex chat registers both its TUI and its app-server; a pi chat its one process.
    for pid, agent_id in (
        (20, "agent-codex"),
        (21, "agent-codex"),
        (30, "agent-pi"),
        (31, "agent-gone"),
    ):
        (registry / f"{pid}.json").write_text(
            json.dumps({"agent_name": "x", "is_worker": False, "agent_id": agent_id})
        )
    (registry / "40.json").write_text(json.dumps({"agent_name": "no-id"}))
    (registry / "notes.txt").write_text("ignored")

    assert programs.agents_by_pid(is_alive=lambda pid: pid != 31) == {
        20: "agent-codex",
        21: "agent-codex",
        30: "agent-pi",
    }


def test_chats_and_code_checkers_are_recognised_by_their_command_lines() -> None:
    assert (
        programs.chat_agent_id(
            "claude --settings /home/user/.mngr/agents/agent-abc/x.json"
        )
        == "agent-abc"
    )
    assert (
        programs.chat_agent_id("claude /home/user/.mngr/agents/agent-abc --resume")
        == "agent-abc"
    )
    assert programs.chat_agent_id("claude --print hi") is None
    for checker in (
        "node /usr/lib/node_modules/pyright/langserver.index.js --stdio",
        "node typescript-language-server --stdio",
        "/home/user/go/bin/gopls serve",
        "/home/user/.cargo/bin/rust-analyzer",
    ):
        assert programs.is_code_checker(checker), checker
    assert not programs.is_code_checker("node /opt/app/server.js")


def test_anonymous_and_shared_memory_are_read_from_smaps_rollup_when_the_kernel_reports_them(
    tmp_path: Path,
) -> None:
    _write_proc_entry(tmp_path, 42, 1, 33236, ["system-monitor"])
    (tmp_path / "42" / "smaps_rollup").write_text(
        "Rss:               33236 kB\nPss:               22440 kB\nPss_Shmem:           512 kB\n"
        "Private_Clean:       100 kB\nPrivate_Dirty:     21640 kB\nAnonymous:         20000 kB\n"
    )
    _write_proc_entry(tmp_path, 43, 1, 1000, ["old-kernel"])
    (tmp_path / "43" / "smaps_rollup").write_text(
        "Rss:  1000 kB\nPss:   900 kB\nPrivate_Dirty: 800 kB\n"
    )

    procs = programs.read_processes(tmp_path)

    assert (procs[42].anonymous, procs[42].shmem) == (20000 * 1024, 512 * 1024)
    assert (procs[43].private, procs[43].anonymous, procs[43].shmem) == (
        800 * 1024,
        None,
        None,
    )


def test_chat_ids_come_from_the_chat_id_label_and_fall_back_to_the_agent_id(
    tmp_path: Path,
) -> None:
    for agent_id, data in (
        ("agent-a", {"labels": {"chat_id": "chat-123", "display_name": "Budget"}}),
        ("agent-b", {"name": "notes"}),
    ):
        (tmp_path / agent_id).mkdir()
        (tmp_path / agent_id / "data.json").write_text(json.dumps(data))

    assert programs.chats(tmp_path) == {
        "agent-a": programs.Chat("Budget", "chat-123"),
        "agent-b": programs.Chat("notes", "agent-b"),
    }


def test_program_names_come_from_the_name_table_then_the_registry_then_the_program(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "apps.toml"
    registry.write_text(
        '[[apps]]\nname = "chat"\ndisplay_name = "Chat"\nprogram = "chat"\npriority = "chat"\n\n'
        '[[apps]]\nname = "system_interface"\ndisplay_name = "Workspace"\nprogram = "system_interface"\n'
        'priority = "system_interface"\n\n'
        '[[apps]]\nname = "budget"\ndisplay_name = "Budget"\nprogram = "budget"\npriority = "user"\n'
    )
    supervised = {
        "chat": 1,
        "system_interface": 2,
        "budget": 3,
        "earlyoom": 4,
        "env-converge": 5,
        "mystery": 6,
    }

    assert programs.program_names(supervised, registry) == {
        "chat": "Chat",
        # The shell's registry row says Workspace, which is the row these items sit under.
        "system_interface": "Desktop",
        "budget": "Budget",
        "earlyoom": "Memory guard",
        "env-converge": "Environment setup",
        "mystery": "mystery",
    }
    assert programs.program_names({"chat": 1}, tmp_path / "missing.toml") == {
        "chat": "chat"
    }

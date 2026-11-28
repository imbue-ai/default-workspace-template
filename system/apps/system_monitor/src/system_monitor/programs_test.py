"""Tests for the records of who runs what: chats, user apps, and the agent registry."""

import json
from pathlib import Path

import pytest
from oom_priority.testing import FAKE_BOOT_ID, write_fake_process

from system_monitor import programs


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
    proc = tmp_path / "proc"
    for pid, agent_id in (
        (20, "agent-codex"),
        (21, "agent-codex"),
        (30, "agent-pi"),
        (31, "agent-gone"),
    ):
        write_fake_process(proc, pid, start_ticks=pid)
        (registry / f"{pid}.json").write_text(
            json.dumps(
                {
                    "agent_name": "x",
                    "is_worker": False,
                    "agent_id": agent_id,
                    "start_ticks": pid,
                    "boot_id": FAKE_BOOT_ID,
                }
            )
        )
    # 31's agent exited and its pid was reused.
    write_fake_process(proc, 31, start_ticks=999)
    write_fake_process(proc, 40, start_ticks=40)
    (registry / "40.json").write_text(
        json.dumps({"agent_name": "no-id", "start_ticks": 40})
    )
    (registry / "notes.txt").write_text("ignored")

    assert programs.agents_by_pid(proc) == {
        20: "agent-codex",
        21: "agent-codex",
        30: "agent-pi",
    }


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


def test_agent_records_are_read_from_the_host_dir_the_environment_names() -> None:
    assert programs.mngr_agents_dir({"MNGR_HOST_DIR": "/mngr"}) == Path("/mngr/agents")
    assert programs.mngr_agents_dir({}) == Path("/home/user/.mngr/agents")

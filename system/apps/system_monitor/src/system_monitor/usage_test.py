"""Tests for the rows of the memory breakdown: the grouping, the exact split, the scaled fallback, and the scratch space."""

from pathlib import Path

from system_monitor import usage
from system_monitor.programs import Process

_MIB = 1024 * 1024


def test_exact_usage_counts_private_memory_per_group_and_shared_memory_once() -> None:
    chat_main = ["claude", "--settings", "/home/user/.mngr/agents/agent-abc/x"]
    procs = {
        10: Process(
            10, 1, 100 * _MIB, ["supervisord"], private=60 * _MIB, pss=70 * _MIB
        ),
        # Two chats' Node processes map the same 200 MB of code: each RSS counts it in full,
        # each PSS carries half of it, and each private part is what stopping it frees.
        20: Process(20, 10, 500 * _MIB, chat_main, private=300 * _MIB, pss=400 * _MIB),
        30: Process(
            30,
            10,
            450 * _MIB,
            ["claude", "/home/user/.mngr/agents/agent-def/x"],
            private=250 * _MIB,
            pss=350 * _MIB,
        ),
        # Not ours to read: it holds memory, but only the System row can account for it.
        40: Process(40, 1, 80 * _MIB, ["sshd"]),
    }
    used = 1000 * _MIB

    groups = usage.exact_usage(
        procs, {"agent-abc": "Budget", "agent-def": "Notes"}, {}, {}, used
    )

    by_id = {group["id"]: group for group in groups}
    assert [group["id"] for group in groups] == [
        "chats",
        "checkers",
        "workspace",
        "apps",
        "shared",
        "system",
    ]
    assert by_id["chats"]["items"] == [
        {"name": "Budget", "bytes": 300 * _MIB},
        {"name": "Notes", "bytes": 250 * _MIB},
    ]
    assert by_id["workspace"]["bytes"] == 60 * _MIB
    assert by_id["shared"]["bytes"] == (10 + 100 + 100) * _MIB
    # Private 610 MB + shared 210 MB held by programs; the remaining 180 MB is the system's.
    assert by_id["system"]["bytes"] == 180 * _MIB
    assert sum(group["bytes"] for group in groups) == used


def test_exact_usage_never_shows_a_negative_system_row() -> None:
    procs = {
        10: Process(10, 1, 900 * _MIB, ["big"], private=800 * _MIB, pss=850 * _MIB)
    }

    groups = usage.exact_usage(procs, {}, {}, {}, used=600 * _MIB)

    assert {group["id"]: group["bytes"] for group in groups}["system"] == 0


def test_memory_is_grouped_by_the_chat_or_app_each_process_descends_from() -> None:
    chat_main = [
        "claude",
        "--settings",
        "/home/user/.mngr/agents/agent-abc/settings.json",
    ]
    procs = {
        1: Process(1, 0, 0, ["tini"]),
        10: Process(10, 1, 100 * _MIB, ["supervisord"]),
        20: Process(20, 10, 500 * _MIB, chat_main),
        21: Process(21, 20, 200 * _MIB, ["pytest"]),
        22: Process(22, 20, 300 * _MIB, ["node", "pyright-langserver", "--stdio"]),
        30: Process(30, 10, 50 * _MIB, ["budget"]),
        31: Process(31, 30, 25 * _MIB, ["worker"]),
        40: Process(40, 10, 70 * _MIB, ["node", "pyright-langserver"]),
    }

    groups = usage.group_usage(
        procs,
        {"agent-abc": "Budget"},
        {"budget": "Budget app"},
        {"budget": 30, "chat": 99},
    )

    by_id = {group["id"]: group for group in groups}
    assert [group["id"] for group in groups] == [
        "chats",
        "checkers",
        "workspace",
        "apps",
    ]
    assert by_id["chats"]["items"] == [{"name": "Budget", "bytes": 700 * _MIB}]
    assert by_id["checkers"]["items"] == [
        {"name": "Budget", "bytes": 300 * _MIB},
        {"name": "Other", "bytes": 70 * _MIB},
    ]
    assert by_id["apps"]["items"] == [{"name": "Budget app", "bytes": 75 * _MIB}]
    assert by_id["workspace"]["bytes"] == 100 * _MIB
    assert by_id["workspace"]["items"] == []
    assert (
        by_id["workspace"]["hint"]
        == "chat app, desktop, built-in apps, background services"
    )


def test_scaling_shrinks_every_figure_to_the_memory_in_use_and_never_grows_them() -> (
    None
):
    def groups() -> list[dict]:
        return [
            {"bytes": 600, "items": [{"name": "a", "bytes": 600}]},
            {"bytes": 400, "items": []},
        ]

    scaled = usage.scale_to_used(groups(), 500)
    assert [group["bytes"] for group in scaled] == [300, 200]
    assert scaled[0]["items"][0]["bytes"] == 300
    assert [group["bytes"] for group in usage.scale_to_used(groups(), 5000)] == [
        600,
        400,
    ]


def test_scratch_space_counts_as_memory_only_when_tmp_is_a_tmpfs(
    tmp_path: Path,
) -> None:
    mounts = tmp_path / "mounts"
    mounts.write_text(
        "/dev/vda1 / ext4 rw 0 0\ntmpfs /tmp tmpfs rw,nosuid,nodev,size=2009212k 0 0\n"
    )
    assert usage.scratch_in_memory(mounts)

    mounts.write_text("/dev/vda1 / ext4 rw 0 0\n/dev/vdb1 /tmp ext4 rw 0 0\n")
    assert not usage.scratch_in_memory(mounts)

    # The last mount on /tmp is the one in effect.
    mounts.write_text("/dev/vdb1 /tmp ext4 rw 0 0\ntmpfs /tmp tmpfs rw 0 0\n")
    assert usage.scratch_in_memory(mounts)
    assert not usage.scratch_in_memory(tmp_path / "missing")


def test_scratch_space_gets_its_own_row_taken_out_of_system() -> None:
    procs = {
        10: Process(10, 1, 500 * _MIB, ["svc"], private=300 * _MIB, pss=400 * _MIB)
    }
    scratch = {"total": 2000 * _MIB, "used": 250 * _MIB, "free": 1750 * _MIB}

    groups = usage.exact_usage(procs, {}, {}, {}, 1000 * _MIB, scratch)

    by_id = {group["id"]: group for group in groups}
    assert [group["id"] for group in groups][-3:] == ["shared", "scratch", "system"]
    assert by_id["scratch"]["bytes"] == 250 * _MIB
    assert by_id["scratch"]["limit"] == 2000 * _MIB
    # 300 private + 100 shared + 250 scratch held; the other 350 MB is the system's.
    assert by_id["system"]["bytes"] == 350 * _MIB
    assert by_id["system"]["hint"] == ""
    assert sum(group["bytes"] for group in groups) == 1000 * _MIB


def test_the_scaled_fallback_shrinks_programs_to_the_memory_left_after_scratch_space() -> (
    None
):
    procs = {10: Process(10, 1, 1600 * _MIB, ["svc"])}
    scratch = {"total": 2000 * _MIB, "used": 200 * _MIB, "free": 1800 * _MIB}

    groups = usage.scaled_usage(procs, {}, {}, {}, 1000 * _MIB, scratch)

    by_id = {group["id"]: group["bytes"] for group in groups}
    assert by_id["workspace"] == 800 * _MIB
    assert by_id["scratch"] == 200 * _MIB
    assert sum(by_id.values()) == 1000 * _MIB


def test_chats_on_any_harness_are_grouped_from_their_registered_main_processes() -> (
    None
):
    # A codex chat registers both its TUI and its app-server; a pi chat its one process.
    agents = {20: "agent-codex", 21: "agent-codex", 30: "agent-pi", 99: "agent-gone"}
    procs = {
        10: Process(10, 1, 100 * _MIB, ["supervisord"]),
        20: Process(20, 10, 300 * _MIB, ["codex", "--remote"]),
        21: Process(21, 10, 200 * _MIB, ["codex", "app-server"]),
        22: Process(22, 21, 50 * _MIB, ["pytest"]),  # started by the codex chat
        30: Process(30, 10, 120 * _MIB, ["node", "/usr/lib/pi/cli.js"]),
    }
    names = {"agent-codex": "Budget", "agent-pi": "Notes"}

    groups = usage.group_usage(procs, names, {}, {}, agents=agents)

    by_id = {group["id"]: group for group in groups}
    assert by_id["chats"]["items"] == [
        {"name": "Budget", "bytes": 550 * _MIB},
        {"name": "Notes", "bytes": 120 * _MIB},
    ]
    assert by_id["workspace"]["bytes"] == 100 * _MIB

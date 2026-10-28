"""Tests for the rows of the breakdown: the grouping, the exact split, the scaled fallback, the scratch
space, the CPU column adding up, and the stop targets the items carry."""

from pathlib import Path

from system_monitor import usage
from system_monitor.programs import Process

_MIB = 1024 * 1024
_CHAT_STOP = {"kind": "chat", "id": "agent-abc"}


def test_exact_usage_counts_anonymous_memory_per_group_and_shared_memory_once() -> None:
    chat_main = ["claude", "--settings", "/home/user/.mngr/agents/agent-abc/x"]
    procs = {
        10: Process(
            10,
            1,
            100 * _MIB,
            ["supervisord"],
            private=90 * _MIB,
            pss=95 * _MIB,
            anonymous=60 * _MIB,
            shmem=10 * _MIB,
        ),
        # Two chats' Node processes map the same 200 MB of code: each RSS counts it in full,
        # each private part holds it where only one maps it, but only what each made for itself
        # (its anonymous memory) is what stopping it frees; the display's buffers they both map
        # are shared memory, counted once through their proportional shares.
        20: Process(
            20,
            10,
            500 * _MIB,
            chat_main,
            private=400 * _MIB,
            pss=450 * _MIB,
            anonymous=300 * _MIB,
            shmem=100 * _MIB,
        ),
        30: Process(
            30,
            10,
            450 * _MIB,
            ["claude", "/home/user/.mngr/agents/agent-def/x"],
            private=350 * _MIB,
            pss=400 * _MIB,
            anonymous=250 * _MIB,
            shmem=100 * _MIB,
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
        {"name": "Budget", "bytes": 300 * _MIB, "cpu": None, "stop": _CHAT_STOP},
        {
            "name": "Notes",
            "bytes": 250 * _MIB,
            "cpu": None,
            "stop": {"kind": "chat", "id": "agent-def"},
        },
    ]
    assert by_id["workspace"]["bytes"] == 60 * _MIB
    assert by_id["workspace"]["items"] == [
        {"name": "Internal system", "bytes": 60 * _MIB, "cpu": None, "stop": None}
    ]
    assert by_id["shared"]["bytes"] == (10 + 100 + 100) * _MIB
    # 610 MB anonymous + 210 MB shared held by programs; the remaining 180 MB is the system's.
    assert by_id["system"]["bytes"] == 180 * _MIB
    assert sum(group["bytes"] for group in groups) == used


def test_a_kernel_without_an_anonymous_line_falls_back_to_private_memory() -> None:
    procs = {
        10: Process(10, 1, 500 * _MIB, ["svc"], private=300 * _MIB, pss=400 * _MIB)
    }

    groups = usage.exact_usage(procs, {}, {}, {}, 1000 * _MIB)

    by_id = {group["id"]: group["bytes"] for group in groups}
    assert by_id["workspace"] == 300 * _MIB
    assert by_id["shared"] == 0
    assert by_id["system"] == 700 * _MIB


def test_exact_usage_never_shows_a_negative_system_row() -> None:
    procs = {
        10: Process(
            10,
            1,
            900 * _MIB,
            ["big"],
            private=800 * _MIB,
            pss=850 * _MIB,
            anonymous=800 * _MIB,
        )
    }

    groups = usage.exact_usage(procs, {}, {}, {}, used=600 * _MIB)

    assert {group["id"]: group["bytes"] for group in groups}["system"] == 0


def test_memory_is_grouped_by_the_chat_app_or_program_each_process_descends_from() -> (
    None
):
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
        # The chat app and a service, supervised programs with names of their own.
        50: Process(50, 10, 120 * _MIB, ["chat-app"]),
        51: Process(51, 50, 30 * _MIB, ["mngr", "observe"]),
        60: Process(60, 10, 40 * _MIB, ["earlyoom"]),
    }

    groups = usage.group_usage(
        procs,
        {"agent-abc": "Budget"},
        {"budget": "Budget app"},
        {"budget": 30, "chat": 50, "earlyoom": 60, "gone": 99},
        chat_ids={"agent-abc": "chat-123"},
        program_names={"chat": "Chat", "earlyoom": "Memory guard", "gone": "Gone"},
    )

    by_id = {group["id"]: group for group in groups}
    assert [group["id"] for group in groups] == [
        "chats",
        "checkers",
        "workspace",
        "apps",
    ]
    assert by_id["chats"]["items"] == [
        {
            "name": "Budget",
            "bytes": 700 * _MIB,
            "cpu": None,
            "stop": {"kind": "chat", "id": "chat-123"},
        }
    ]
    assert by_id["checkers"]["items"] == [
        {
            "name": "Budget",
            "bytes": 300 * _MIB,
            "cpu": None,
            "stop": {"kind": "checkers", "id": "agent-abc"},
        },
        {"name": "Other", "bytes": 70 * _MIB, "cpu": None, "stop": None},
    ]
    assert by_id["apps"]["items"] == [
        {
            "name": "Budget app",
            "bytes": 75 * _MIB,
            "cpu": None,
            "stop": {"kind": "app", "id": "budget"},
        }
    ]
    # The workspace's own programs, largest first, and what no program started by its command;
    # none of them can be stopped from the page.
    assert by_id["workspace"]["bytes"] == 290 * _MIB
    assert by_id["workspace"]["items"] == [
        {"name": "Chat", "bytes": 150 * _MIB, "cpu": None, "stop": None},
        {"name": "Internal system", "bytes": 100 * _MIB, "cpu": None, "stop": None},
        {"name": "Memory guard", "bytes": 40 * _MIB, "cpu": None, "stop": None},
    ]
    assert (
        by_id["workspace"]["hint"]
        == "chat app, desktop, built-in apps, background services"
    )


def test_scaling_shrinks_every_figure_to_the_memory_in_use_and_never_grows_them() -> (
    None
):
    def groups() -> list[dict]:
        return [
            {"bytes": 600, "items": [{"name": "a", "bytes": 600, "cpu": None}]},
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
        10: Process(
            10,
            1,
            500 * _MIB,
            ["svc"],
            private=300 * _MIB,
            pss=400 * _MIB,
            anonymous=300 * _MIB,
            shmem=100 * _MIB,
        )
    }
    scratch = {"total": 2000 * _MIB, "used": 250 * _MIB, "free": 1750 * _MIB}

    groups = usage.exact_usage(procs, {}, {}, {}, 1000 * _MIB, scratch)

    by_id = {group["id"]: group for group in groups}
    assert [group["id"] for group in groups][-3:] == ["shared", "scratch", "system"]
    assert by_id["scratch"]["bytes"] == 250 * _MIB
    assert by_id["scratch"]["limit"] == 2000 * _MIB
    # 300 anonymous + 100 shared + 250 scratch held; the other 350 MB is the system's.
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
    # Shrunk to fit, the programs leave nothing for the System row.
    assert by_id["system"] == 0
    assert sum(by_id.values()) == 1000 * _MIB


def test_the_scaled_fallback_gives_the_system_row_what_the_programs_do_not_account_for() -> (
    None
):
    procs = {10: Process(10, 1, 300 * _MIB, ["svc"])}

    groups = usage.scaled_usage(procs, {}, {}, {}, 1000 * _MIB)

    by_id = {group["id"]: group["bytes"] for group in groups}
    assert by_id["workspace"] == 300 * _MIB
    assert by_id["system"] == 700 * _MIB
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
        {
            "name": "Budget",
            "bytes": 550 * _MIB,
            "cpu": None,
            "stop": {"kind": "chat", "id": "agent-codex"},
        },
        {
            "name": "Notes",
            "bytes": 120 * _MIB,
            "cpu": None,
            "stop": {"kind": "chat", "id": "agent-pi"},
        },
    ]
    assert by_id["workspace"]["bytes"] == 100 * _MIB


def test_cpu_shares_roll_up_to_the_group_of_whatever_started_the_process() -> None:
    chat_main = ["claude", "--settings", "/home/user/.mngr/agents/agent-abc/x"]
    procs = {
        10: Process(
            10, 1, 100 * _MIB, ["supervisord"], private=60 * _MIB, pss=70 * _MIB
        ),
        20: Process(20, 10, 500 * _MIB, chat_main, private=300 * _MIB, pss=400 * _MIB),
        # The chat's test run: its memory and its CPU land with the chat.
        21: Process(21, 20, 50 * _MIB, ["pytest"], private=40 * _MIB, pss=45 * _MIB),
        # A process with CPU but no memory of its own still shows in the breakdown's CPU.
        30: Process(30, 10, 0, ["node", "server.js"], private=0, pss=0),
    }

    groups = usage.exact_usage(
        procs,
        {"agent-abc": "Budget"},
        {"notes": "Notes"},
        {"notes": 30},
        1000 * _MIB,
        cpu_by_pid={20: 0.2, 21: 0.05, 30: 0.01, 10: 0.004},
    )

    by_id = {group["id"]: group for group in groups}
    assert by_id["chats"]["cpu"] == 0.25
    assert by_id["chats"]["items"] == [
        {"name": "Budget", "bytes": 340 * _MIB, "cpu": 0.25, "stop": _CHAT_STOP}
    ]
    assert by_id["apps"]["cpu"] == 0.01
    assert by_id["workspace"]["cpu"] == 0.004
    # Without the whole to compare with, the System row claims no share either.
    assert by_id["shared"]["cpu"] is None and by_id["system"]["cpu"] is None
    # Without a reading, no row claims a share.
    without = {
        group["id"]: group["cpu"]
        for group in usage.exact_usage(
            procs, {"agent-abc": "Budget"}, {}, {}, 1000 * _MIB
        )
    }
    assert set(without.values()) == {None}


def test_the_system_row_takes_the_cpu_the_rows_fall_short_of_the_whole() -> None:
    procs = {
        10: Process(
            10, 1, 100 * _MIB, ["supervisord"], private=60 * _MIB, pss=70 * _MIB
        ),
        20: Process(20, 10, 50 * _MIB, ["svc"], private=40 * _MIB, pss=45 * _MIB),
    }
    # The sampler leaves shares under a tenth of a percent out of its file, so the rows sum to
    # less than the whole; the difference is the System row's.
    exact = usage.exact_usage(
        procs, {}, {}, {}, 1000 * _MIB, cpu_by_pid={10: 0.01, 20: 0.02}, cpu_usage=0.05
    )
    scaled = usage.scaled_usage(
        procs, {}, {}, {}, 1000 * _MIB, cpu_by_pid={10: 0.01, 20: 0.02}, cpu_usage=0.05
    )

    for groups in (exact, scaled):
        by_id = {group["id"]: group["cpu"] for group in groups}
        assert by_id["workspace"] == 0.03
        assert by_id["system"] == 0.02
        assert sum(cpu for cpu in by_id.values() if cpu is not None) == 0.05
    # The rows can round past the whole; the System row never goes negative.
    over = usage.exact_usage(
        procs, {}, {}, {}, 1000 * _MIB, cpu_by_pid={10: 0.03, 20: 0.03}, cpu_usage=0.05
    )
    assert {group["id"]: group["cpu"] for group in over}["system"] == 0.0


def test_a_chats_code_checkers_are_found_by_pid_for_stopping() -> None:
    chat_main = ["claude", "--settings", "/home/user/.mngr/agents/agent-abc/x"]
    procs = {
        10: Process(10, 1, 0, ["supervisord"]),
        20: Process(20, 10, 0, chat_main),
        21: Process(21, 20, 0, ["node", "pyright-langserver", "--stdio"]),
        22: Process(22, 21, 0, ["node", "pyright-langserver", "--worker"]),
        23: Process(23, 20, 0, ["pytest"]),
        30: Process(30, 10, 0, ["codex"]),
        31: Process(31, 30, 0, ["rust-analyzer"]),
        40: Process(40, 10, 0, ["node", "pyright-langserver"]),
    }

    assert usage.checker_pids_of_chat(procs, {30: "agent-codex"}, "agent-abc") == [
        21,
        22,
    ]
    assert usage.checker_pids_of_chat(procs, {30: "agent-codex"}, "agent-codex") == [31]
    assert usage.checker_pids_of_chat(procs, {}, "agent-nobody") == []


def test_processes_no_program_started_are_named_by_their_command() -> None:
    procs = {
        1: Process(1, 0, 10 * _MIB, ["/sbin/tini", "--"]),
        5: Process(5, 1, 20 * _MIB, ["sshd: /usr/sbin/sshd -D"]),
        6: Process(6, 5, 15 * _MIB, ["sshd-session: root [priv]"]),
        10: Process(10, 1, 100 * _MIB, ["uv", "run", "bootstrap"]),
        11: Process(
            11, 10, 40 * _MIB, ["/usr/bin/python3", "/usr/bin/supervisord", "-n"]
        ),
        12: Process(12, 1, 12 * _MIB, ["tmux", "new-session", "-d"]),
        13: Process(13, 12, 5 * _MIB, ["/bin/bash"]),
        20: Process(20, 1, 30 * _MIB, ["mystery-daemon"]),
    }

    groups = usage.group_usage(procs, {}, {}, {})

    by_id = {group["id"]: group for group in groups}
    assert [
        (item["name"], item["bytes"] // _MIB) for item in by_id["workspace"]["items"]
    ] == [
        ("Internal system", 167),
        ("SSH access", 35),
        ("Other", 30),
    ]

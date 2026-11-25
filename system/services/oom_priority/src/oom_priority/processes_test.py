"""Tests for the process reader and for who each process belongs to."""

from pathlib import Path

from oom_priority import processes
from oom_priority.processes import Owner, OwnerKind, Process


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

    procs = processes.read_processes(tmp_path)

    assert list(procs) == [42]
    assert (procs[42].ppid, procs[42].rss, procs[42].argv) == (
        1,
        2048 * 1024,
        ["python3", "-m", "thing"],
    )
    # No smaps_rollup (a kernel without one, or a process not ours to read): no breakdown.
    assert (procs[42].private, procs[42].pss) == (None, None)
    assert not processes.has_breakdown(procs)


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

    procs = processes.read_processes(tmp_path)

    assert (procs[42].private, procs[42].pss) == (21740 * 1024, 22440 * 1024)
    assert processes.has_breakdown(procs)


def test_chats_and_code_checkers_are_recognised_by_their_command_lines() -> None:
    assert (
        processes.chat_agent_id(
            "claude --settings /home/user/.mngr/agents/agent-abc/x.json"
        )
        == "agent-abc"
    )
    assert (
        processes.chat_agent_id("claude /home/user/.mngr/agents/agent-abc --resume")
        == "agent-abc"
    )
    # A Modal sandbox keeps mngr's host dir at /mngr rather than under a home directory.
    assert (
        processes.chat_agent_id("claude --settings /mngr/agents/agent-abc/x.json")
        == "agent-abc"
    )
    assert processes.chat_agent_id("claude --print hi") is None
    for checker in (
        "node /usr/lib/node_modules/pyright/langserver.index.js --stdio",
        "node typescript-language-server --stdio",
        "/home/user/go/bin/gopls serve",
        "/home/user/.cargo/bin/rust-analyzer",
    ):
        assert processes.is_code_checker(checker), checker
    assert not processes.is_code_checker("node /opt/app/server.js")


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

    procs = processes.read_processes(tmp_path)

    assert (procs[42].anonymous, procs[42].shmem) == (20000 * 1024, 512 * 1024)
    assert (procs[43].private, procs[43].anonymous, procs[43].shmem) == (
        800 * 1024,
        None,
        None,
    )


def _write_gvisor_smaps(
    proc: Path, pid: int, own_anonymous_kib: int, mapped: list[tuple[str, str, int]]
) -> None:
    """A gVisor-style smaps: Chromium's binary resident in full, the process's own heap, and the
    shared segments (``(device, inode, resident KiB)``) it maps. No smaps_rollup."""
    lines = [
        "00400000-1d000000 r-xp 00000000 00:2a 1234 /opt/fortress/chrome",
        "Rss:   409600 kB",
        "Pss:   409600 kB",
        "Anonymous:   0 kB",
        "1d000000-1e000000 rw-p 00000000 00:00 0 [heap]",
        f"Rss:   {own_anonymous_kib} kB",
        f"Pss:   {own_anonymous_kib} kB",
        f"Anonymous:   {own_anonymous_kib} kB",
    ]
    for index, (device, inode, rss_kib) in enumerate(mapped):
        lines += [
            f"7f0{index}0000-7f0{index}f000 rw-s 00000000 {device} {inode} /memfd:shared-memory-region (deleted)",
            f"Rss:   {rss_kib} kB",
            f"Pss:   {rss_kib} kB",
            "Anonymous:   0 kB",
        ]
    (proc / str(pid) / "smaps").write_text("\n".join(lines) + "\n")


def test_under_gvisor_each_process_holds_its_anonymous_memory_and_a_shared_segment_counts_once(
    tmp_path: Path,
) -> None:
    # Three Chromium processes under gVisor: each one's resident size counts the 400 MiB binary it
    # maps, and gVisor's Pss equals its Rss, so only Anonymous says what each made for itself.
    for pid, anonymous_kib in ((100, 51200), (101, 30720), (102, 20480)):
        _write_proc_entry(
            tmp_path, pid, 1, 409600 + anonymous_kib, ["/opt/fortress/chrome"]
        )
    # Each process's resident size of the memfd covers only the pages it touched; 101 touched it all.
    _write_gvisor_smaps(tmp_path, 100, 51200, [("00:05", "77", 16384)])
    _write_gvisor_smaps(
        tmp_path, 101, 30720, [("00:05", "77", 20480), ("00:05", "78", 4096)]
    )
    _write_gvisor_smaps(tmp_path, 102, 20480, [("00:05", "77", 16384)])

    procs = processes.read_processes(tmp_path)

    assert processes.has_breakdown(procs)
    assert [procs[pid].anonymous for pid in (100, 101, 102)] == [
        51200 * 1024,
        30720 * 1024,
        20480 * 1024,
    ]
    # The memfd every process maps is held once, at the largest resident size any reports (20 MiB),
    # beside the one only 101 maps (4 MiB).
    assert sum(p.shmem or 0 for p in procs.values()) == 24 * 1024 * 1024


def test_a_process_belongs_to_the_nearest_ancestor_that_starts_an_owners_tree() -> None:
    profile = "--user-data-dir=/home/user/.mngr/browser-profiles/browser-use-user-data-dir-browser-1"
    procs = {
        1: Process(1, 0, 1, ["tini"]),
        10: Process(10, 1, 1, ["python3", "/usr/bin/supervisord"]),
        20: Process(20, 10, 1, ["screen-service"]),
        # The Screen's service starts the browser, whose renderer carries no profile of its own.
        21: Process(21, 20, 1, ["/opt/fortress/chrome", profile]),
        22: Process(22, 21, 1, ["/opt/fortress/chrome", "--type=renderer"]),
        30: Process(30, 10, 1, ["notes-app"]),
        40: Process(40, 1, 1, ["codex", "--remote"]),
        # A chat's own test browser is the chat's: its profile is not one of the Screen's.
        41: Process(41, 40, 1, ["chrome", "--user-data-dir=/tmp/playwright-profile"]),
        50: Process(50, 1, 1, ["sshd: user@pts/0"]),
    }
    roots = processes.owner_roots(
        procs, {40: "agent-codex", 99: "agent-gone"}, {"screen": 20, "notes": 30}
    )

    owners = {pid: processes.find_owner(pid, procs, roots) for pid in procs}

    assert owners == {
        1: None,
        10: None,
        20: Owner(OwnerKind.PROGRAM, "screen"),
        21: Owner(OwnerKind.BROWSER, "browser-1"),
        22: Owner(OwnerKind.BROWSER, "browser-1"),
        30: Owner(OwnerKind.PROGRAM, "notes"),
        40: Owner(OwnerKind.AGENT, "agent-codex"),
        41: Owner(OwnerKind.AGENT, "agent-codex"),
        50: None,
    }
    assert processes.unowned_item(procs[50]) == processes.SSH_ITEM
    assert processes.unowned_item(procs[10]) == processes.INTERNAL_ITEM


def test_a_browser_is_named_by_the_whole_last_component_of_its_profile() -> None:
    root = "/home/user/.mngr/browser-profiles/browser-use-user-data-dir-"
    procs = {
        1: Process(1, 0, 1, ["chrome", f"--user-data-dir={root}browser-1"]),
        2: Process(2, 0, 1, ["chrome", f"--user-data-dir={root}browser-10/"]),
        3: Process(3, 0, 1, ["chrome", f"--disk-cache-dir={root}browser-1"]),
    }

    assert processes.browser_pids(procs) == {1: "browser-1", 2: "browser-10"}

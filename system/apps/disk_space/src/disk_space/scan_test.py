"""Measuring the disk: which folder gets which verdict, and the tree the page draws from a measurement.

The measurements here are built from planted sizes rather than a real ``du``: the point is what the app decides about
a folder, and a test that walked the real disk would measure the machine it runs on instead. The workspace's own
paths come from ``scan`` (it resolves where the workspace is), never spelled out here.
"""

import os
import time
from pathlib import Path

import pytest
from disk_headroom.space import DiskSpace

from disk_space import scan, store
from disk_space.testing import (
    GIB,
    a_disk,
    a_rule,
    install_disk,
    install_fake_du,
    install_groups,
    latest_measurement,
)

WORKSPACE = scan.WORKSPACE
HOME = scan.HOME


def _old(path: Path, *, days: int) -> Path:
    """A file ``days`` old, so the stale-window check has something to judge."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    stamp = time.time() - days * 86400
    os.utime(path, (stamp, stamp))
    return path


def _node(nodes: list[dict], name: str) -> dict:
    return next(node for node in nodes if node["name"] == name)


# What the app says about each folder.


@pytest.mark.parametrize(
    ("path", "verdict", "method"),
    [
        ("/var/cache/user/uv", scan.SAFE, "uv-cache"),
        ("/var/cache/user/huggingface", scan.SAFE, "remove"),
        ("/root/.npm/_cacache", scan.SAFE, "remove"),
        (f"{HOME}/.npm/_cacache", scan.SAFE, "remove"),
        (f"{WORKSPACE}/data/uploads", scan.ASK, "remove"),
        (f"{WORKSPACE}/data/my-folder", scan.ASK, "remove"),
        (f"{WORKSPACE}/data/.apps/my-app", scan.ASK, "remove"),
        (f"{HOME}/.mngr/agents/agent-1", scan.ASK, ""),
        (f"{HOME}/worktrees/task-1", scan.ASK, "remove"),
        (f"{WORKSPACE}/.venv", scan.KEEP, ""),
        (f"{WORKSPACE}/system", scan.KEEP, ""),
        (f"{WORKSPACE}/data/memories", scan.KEEP, ""),
        (f"{WORKSPACE}/data/.state", scan.KEEP, ""),
        (f"{WORKSPACE}/.external_worktrees/thing", scan.ASK, "remove"),
        ("/root/.cache/pip", scan.SAFE, "remove"),
    ],
)
def test_a_folder_gets_the_verdict_its_rule_gives_it(
    path: str, verdict: str, method: str
) -> None:
    rule = a_rule(Path(path))
    assert rule.verdict == verdict
    assert rule.method == method


def test_a_folder_named_like_a_rule_at_another_depth_is_not_decided_by_it() -> None:
    # A rule names one folder at one depth: the workspace's data folder decides about its own children, and about
    # nothing deeper, so an app's data inside it is judged by the rule for that app, not by "data/*".
    assert scan.rule_for(WORKSPACE / "data" / "my-folder" / "deeper") is None
    assert scan.rule_for(Path("/var/cache/user/uv/archive-v0/x")) is None
    assert scan.rule_for(Path("/srv/anything")) is None


def test_a_container_rule_only_names_a_folder_and_decides_nothing_itself() -> None:
    assert a_rule(WORKSPACE / "data").verdict == scan.CONTAINER
    assert a_rule(WORKSPACE / "data" / ".apps").verdict == scan.CONTAINER
    assert a_rule(HOME / ".mngr" / "agents").verdict == scan.CONTAINER
    assert a_rule(Path("/var/tmp")).verdict == scan.CONTAINER


def test_the_first_matching_rule_decides_a_folder_whose_keep_rule_comes_after_the_container() -> (
    None
):
    # ``data/.*`` would make ``data/.state`` workspace data; the keep rule naming it comes first.
    assert a_rule(WORKSPACE / "data" / ".state").verdict == scan.KEEP
    assert a_rule(WORKSPACE / "data" / ".apps").verdict == scan.CONTAINER


def test_a_rule_reads_a_chats_name_from_its_record() -> None:
    rule = a_rule(HOME / ".mngr" / "agents" / "agent-1")
    chat = store.data_dir() / "chat-fixture"
    chat.mkdir(parents=True)
    assert rule.label(chat).startswith("Chat files")
    (chat / "data.json").write_text('{"labels": {"display_name": "Gardening"}}')
    assert rule.label(chat) == "Chat: Gardening"
    (chat / "data.json").write_text('{"name": "Plain name"}')
    assert rule.label(chat) == "Chat: Plain name"
    (chat / "data.json").write_text("not json")
    assert rule.label(chat).startswith("Chat files")


# Scratch space: the one verdict decided by what is in the folder, not by its name.


def test_a_scratch_folder_nothing_touched_for_a_week_is_safe_to_delete(
    tmp_path: Path,
) -> None:
    old = tmp_path / "stale-build"
    _old(old / "output.txt", days=scan.SCRATCH_STALE_DAYS + 1)
    assert scan.scratch_verdict(old, busy=set()) == (
        scan.SAFE,
        "Scratch: stale-build",
        f"Leftover scratch files nothing has touched in over {scan.SCRATCH_STALE_DAYS} days.",
    )


def test_a_scratch_folder_touched_this_week_is_ask_first(tmp_path: Path) -> None:
    recent = tmp_path / "recent"
    _old(recent / "output.txt", days=1)
    verdict, _, why = scan.scratch_verdict(recent, busy=set())
    assert verdict == scan.ASK
    assert "recent work" in why


def test_a_scratch_folder_something_runs_in_is_never_safe(tmp_path: Path) -> None:
    old = tmp_path / "in-use"
    _old(old / "output.txt", days=scan.SCRATCH_STALE_DAYS + 1)
    child = old / "subdir"
    child.mkdir()
    verdict, _, why = scan.scratch_verdict(old, busy={child})
    assert verdict == scan.ASK
    assert "running in it" in why
    assert scan.scratch_verdict(child, busy={child})[0] == scan.ASK


def test_a_scratch_folder_holding_project_history_is_ask_first(tmp_path: Path) -> None:
    project = tmp_path / "clone"
    _old(project / "README.md", days=scan.SCRATCH_STALE_DAYS + 30)
    (project / ".git").mkdir()
    verdict, _, why = scan.scratch_verdict(project, busy=set())
    assert verdict == scan.ASK
    assert "changes saved nowhere else" in why


def test_the_stale_check_stops_at_the_first_recent_entry(tmp_path: Path) -> None:
    folder = tmp_path / "mixed"
    _old(folder / "old.txt", days=30)
    (folder / "fresh.txt").write_text("x")
    assert scan._changed_recently(folder) is True


def test_the_stale_check_gives_up_on_a_folder_too_big_to_judge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    folder = tmp_path / "huge"
    for index in range(6):
        _old(folder / f"file-{index}.txt", days=30)
    monkeypatch.setattr(scan, "CHANGED_CHECK_ENTRIES", 3)
    # Past the cap the folder is treated as still in use, never as safe to delete.
    assert scan._changed_recently(folder) is True
    monkeypatch.setattr(scan, "CHANGED_CHECK_ENTRIES", 60)
    assert scan._changed_recently(folder) is False


def test_the_stale_check_finishes_on_a_folder_holding_a_link_back_to_itself(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "looping"
    _old(folder / "old.txt", days=30)
    link = folder / "loop"
    link.symlink_to(folder)
    stamp = time.time() - 30 * 86400
    os.utime(link, (stamp, stamp), follow_symlinks=False)
    assert scan._changed_recently(folder) is False


def test_a_link_created_just_now_counts_as_a_recent_change(tmp_path: Path) -> None:
    folder = tmp_path / "linked"
    _old(folder / "old.txt", days=30)
    (folder / "link.txt").symlink_to(tmp_path / "elsewhere.txt")
    assert scan._changed_recently(folder) is True


def test_a_scratch_folder_that_cannot_be_read_is_never_called_safe(
    tmp_path: Path,
) -> None:
    assert scan._changed_recently(tmp_path / "gone") is True


def test_busy_folders_include_where_this_process_runs() -> None:
    assert Path.cwd().resolve() in scan.busy_folders()


# The tree the page draws.


def _tree(
    sizes: dict[Path, int], top: Path, *, busy: set[Path] | None = None
) -> list[dict]:
    """A measurement's tree for a group whose one top folder is ``top``, built the way ``measure`` builds it."""
    builder = scan.TreeBuilder(busy=set() if busy is None else busy)
    root = builder.add(None, "Whole disk", "", sizes[top], scan.KEEP, "")
    builder.add_path(root, top, sizes, None)
    scan._fill_group_verdicts(builder.nodes)
    return builder.nodes


def _workspace_tree() -> list[dict]:
    """A measurement's tree for a small workspace group."""
    sizes = {
        WORKSPACE: 100 * GIB,
        WORKSPACE / "data": 60 * GIB,
        WORKSPACE / "data" / ".apps": 45 * GIB,
        WORKSPACE / "data" / ".apps" / "bookshelf": 45 * GIB,
        WORKSPACE / "data" / "reports": 10 * GIB,
        WORKSPACE / "data" / ".state": 4 * GIB,
        WORKSPACE / "system": 30 * GIB,
        WORKSPACE / ".venv": 10 * GIB,
    }
    return _tree(sizes, WORKSPACE)


def test_the_lists_are_the_folders_a_rule_decided_and_nothing_else() -> None:
    nodes = _workspace_tree()
    items = {node["name"]: node for node in nodes if node["item"]}
    assert "bookshelf app's data" in items
    assert items["bookshelf app's data"]["verdict"] == scan.ASK
    assert items["Your folder: reports"]["verdict"] == scan.ASK
    assert items["Workspace settings and state"]["verdict"] == scan.KEEP
    # A container only names a folder: its children are the items, so it is never one itself.
    assert "Apps' stored data" not in items
    assert "Your data" not in items
    assert all(not node["deletable"] for node in items.values())


def test_something_running_in_a_folder_moves_it_from_safe_to_ask() -> None:
    sizes = {
        Path("/var/cache"): 10 * GIB,
        Path("/var/cache/user"): 8 * GIB,
        Path("/var/cache/user/huggingface"): 8 * GIB,
    }
    busy = {Path("/var/cache/user/huggingface")}
    models = _node(_tree(sizes, Path("/var/cache"), busy=busy), "AI model downloads")
    assert models["verdict"] == scan.ASK
    assert models["why"] == "Something is running in it right now."
    assert models["deletable"] is False


def test_a_container_reads_as_the_verdict_holding_most_of_its_space() -> None:
    sizes = {
        WORKSPACE: 100 * GIB,
        WORKSPACE / "system": 30 * GIB,
        WORKSPACE / "data": 60 * GIB,
        WORKSPACE / "data" / "notes": 50 * GIB,
        WORKSPACE / "data" / ".extra": 8 * GIB,
    }
    data = _node(_tree(sizes, WORKSPACE), "Your data")
    # Most of the space in it belongs to a folder of the user's own, so the container is drawn as Ask first, and
    # nothing in it is counted safe.
    assert data["item"] is False
    assert data["verdict"] == scan.ASK
    assert data["safe_bytes"] == 0


def test_folders_too_small_to_show_are_gathered_into_one_node() -> None:
    sizes = {
        WORKSPACE: 100 * GIB,
        WORKSPACE / "data": 60 * GIB,
        WORKSPACE / "data" / "notes": 50 * GIB,
        WORKSPACE / "data" / "tiny": scan.MIN_NODE_BYTES // 2,
    }
    nodes = _tree(sizes, WORKSPACE)
    data = _node(nodes, "Your data")
    children = {node["name"]: node for node in nodes if node["parent"] == data["id"]}
    assert _node(nodes, "Your folder: notes")["bytes"] == 50 * GIB
    assert children["Smaller items"]["bytes"] == 60 * GIB - 50 * GIB


def test_the_safe_share_of_a_folder_adds_up_for_the_treemap() -> None:
    sizes = {
        Path("/var/cache"): 10 * GIB,
        Path("/var/cache/user"): 8 * GIB,
        Path("/var/cache/user/uv"): 6 * GIB,
        Path("/var/cache/user/restic"): 2 * GIB,
    }
    nodes = _tree(sizes, Path("/var/cache"))
    assert _node(nodes, "Whole disk")["safe_bytes"] == 8 * GIB
    assert _node(nodes, "System caches")["safe_bytes"] == 8 * GIB
    assert _node(nodes, "Download caches")["verdict"] == scan.SAFE


def test_only_a_safe_item_with_a_way_to_delete_it_is_deletable() -> None:
    nodes = _workspace_tree()
    assert all(
        node["deletable"] == (node["item"] and node["verdict"] == scan.SAFE)
        for node in nodes
    )


# A whole measurement, against planted sizes.


def test_a_measurement_records_every_group_and_the_raw_output_behind_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    groups = (
        scan.Group("workspace", "Your workspace", (WORKSPACE,), 3),
        scan.Group("caches", "Download caches", (Path("/var/cache"),), 3),
        scan.Group("system", "System software", (Path("/usr"),), 1),
    )
    install_groups(monkeypatch, *groups)
    install_fake_du(
        monkeypatch,
        {
            "workspace": {WORKSPACE: 40 * GIB, WORKSPACE / "system": 30 * GIB},
            "caches": {
                Path("/var/cache"): 10 * GIB,
                Path("/var/cache/user/uv"): 10 * GIB,
            },
            "system": {Path("/usr"): 30 * GIB},
        },
    )
    install_disk(monkeypatch, a_disk())
    monkeypatch.setattr(scan, "busy_folders", lambda: set())
    snapshot = scan.measure()
    groups_shown = {node["name"] for node in snapshot["nodes"]}
    assert {"Your workspace", "Download caches", "System software"} <= groups_shown
    saved = store.snapshots_dir() / snapshot["id"]
    assert sorted(path.name for path in saved.iterdir()) == [
        "du-caches.txt",
        "du-system.txt",
        "du-workspace.txt",
        "snapshot.json",
    ]
    assert (
        (saved / "du-workspace.txt")
        .read_text()
        .startswith("faked du output for workspace")
    )
    assert latest_measurement()["id"] == snapshot["id"]
    assert snapshot["missing"] == []


def test_a_group_whose_du_failed_is_named_rather_than_counted_as_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_groups(
        monkeypatch,
        scan.Group("workspace", "Your workspace", (WORKSPACE,), 3),
        scan.Group("system", "System software", (Path("/usr"),), 1),
    )
    install_fake_du(
        monkeypatch,
        {
            "workspace": {WORKSPACE: 40 * GIB, WORKSPACE / "system": 30 * GIB},
            "system": None,
        },
    )
    install_disk(monkeypatch, a_disk())
    monkeypatch.setattr(scan, "busy_folders", lambda: set())
    snapshot = scan.measure()
    assert snapshot["missing"] == ["System software"]
    assert "System software" not in {node["name"] for node in snapshot["nodes"]}


def test_the_space_no_group_accounts_for_is_shown_as_everything_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_groups(
        monkeypatch, scan.Group("workspace", "Your workspace", (WORKSPACE,), 3)
    )
    install_fake_du(
        monkeypatch,
        {"workspace": {WORKSPACE: 40 * GIB, WORKSPACE / "system": 30 * GIB}},
    )
    install_disk(monkeypatch, a_disk())
    monkeypatch.setattr(scan, "busy_folders", lambda: set())
    snapshot = scan.measure()
    everything_else = _node(snapshot["nodes"], "Everything else")
    assert everything_else["bytes"] == 93 * GIB - 40 * GIB
    root = snapshot["nodes"][0]
    assert root["bytes"] == sum(
        node["bytes"] for node in snapshot["nodes"] if node["parent"] == root["id"]
    )


def test_a_group_of_several_top_folders_judges_each_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_groups(
        monkeypatch,
        scan.Group(
            "caches", "Download caches", (Path("/var/cache"), HOME / ".cache"), 3
        ),
    )
    install_fake_du(
        monkeypatch,
        {
            "caches": {
                Path("/var/cache"): 10 * GIB,
                Path("/var/cache/user"): 8 * GIB,
                Path("/var/cache/user/uv"): 8 * GIB,
                HOME / ".cache": 2 * GIB,
                HOME / ".cache" / "pip": 2 * GIB,
            }
        },
    )
    install_disk(monkeypatch, a_disk())
    monkeypatch.setattr(scan, "busy_folders", lambda: set())
    snapshot = scan.measure()
    caches = _node(snapshot["nodes"], "Download caches")
    assert caches["item"] is False
    children = {
        node["name"]: node
        for node in snapshot["nodes"]
        if node["parent"] == caches["id"]
    }
    # The containers are drawn as the verdict holding most of their space, so the group reads as safe to trim.
    assert children["System caches"]["verdict"] == scan.SAFE
    assert children["System caches"]["safe_bytes"] == 8 * GIB
    uv = _node(snapshot["nodes"], "Python package downloads")
    assert uv["verdict"] == scan.SAFE
    assert uv["deletable"] is True
    assert uv["method"] == "uv-cache"
    pip = [
        node
        for node in snapshot["nodes"]
        if node["name"] == "Python package downloads" and node is not uv
    ]
    assert pip and pip[0]["verdict"] == scan.SAFE


def test_the_disk_figures_are_the_workspace_disk_as_it_can_be_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A cloud machine's quota (40 GB, 32.4 used) runs out before the filesystem does: the figures are the quota's.
    gb = 1024**3
    monkeypatch.setattr(
        scan,
        "read_disk_space",
        lambda _path: DiskSpace(
            total=40 * gb,
            used=int(32.4 * gb),
            free=int(7.6 * gb),
            filesystem_free=9 * gb,
            quota_free=int(7.6 * gb),
        ),
    )
    figures = scan.disk_usage()
    assert figures == {
        "total": 40 * gb,
        "used": int(32.4 * gb),
        "free": int(7.6 * gb),
        "percent": 81,
    }


def test_the_real_disk_figures_are_consistent() -> None:
    figures = scan.disk_usage()
    assert figures["total"] > 0
    assert figures["used"] + figures["free"] <= figures["total"]
    assert 0 <= figures["percent"] <= 100


def test_a_measurement_that_is_already_running_is_not_started_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scan, "measure", lambda: {"id": "measured"})
    with store.held_lock("scan", wait=False) as held:
        assert held is True
        assert scan.scan_locked() is None
    assert scan.scan_locked() == {"id": "measured"}
    assert store.read_watch().get("scan_started_at") is None

"""Tests for the venv links: each project's ``.venv`` a symlink to a target beside the uv cache.

uv is stood in for by a recorder: ``uv cache dir`` answers with a cache under the test's
own directory, and a sync writes a ``bin/python`` into the venv it was run against, the
way a real one leaves the venv valid.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

import pytest

from workspace_bare_scripts import venv_links


class _Completed(NamedTuple):
    returncode: int
    stdout: str
    stderr: str


class _FakeUv:
    """Answers ``uv cache dir`` and stands in for a sync, recording every call."""

    def __init__(self, cache_dir: Path, *, sync_fails: bool = False) -> None:
        self.cache_dir = cache_dir
        self.sync_fails = sync_fails
        self.calls: list[tuple[list[str], Path]] = []
        self.venv_during_sync: list[Path] = []

    def __call__(self, command: Sequence[str], /, *, cwd: Path) -> _Completed:
        argv = list(command)
        self.calls.append((argv, cwd))
        if argv == ["uv", "cache", "dir"]:
            return _Completed(0, f"{self.cache_dir}\n", "")
        if argv[:2] == ["uv", "sync"]:
            venv = cwd / venv_links.VENV_NAME
            self.venv_during_sync.append(Path(os.path.realpath(venv)))
            if self.sync_fails:
                return _Completed(2, "", "error: no network")
            (venv / "bin").mkdir(parents=True, exist_ok=True)
            (venv / "bin" / "python").write_text("")
            return _Completed(0, "", "")
        raise AssertionError(f"unexpected command {argv}")


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    path = tmp_path / "cache" / "uv"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def root(cache_dir: Path) -> Path:
    return venv_links.venvs_root(cache_dir)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    path = tmp_path / "workspace"
    path.mkdir()
    return path


def _never_alive(pid: int) -> bool:
    return False


def test_ensure_links_a_new_project_to_an_empty_target_with_an_owner_record(
    project: Path, cache_dir: Path, root: Path
) -> None:
    uv = _FakeUv(cache_dir)

    venv_links.ensure(project, [], run=uv, alive=_never_alive)

    link = project / ".venv"
    target = venv_links.target_for(root, project)
    assert link.is_symlink()
    assert os.path.realpath(link) == os.path.realpath(target)
    assert target.is_dir() and list(target.iterdir()) == []
    assert venv_links.read_owner(target) == venv_links.Owner(
        link=str(link), holder_pid=None
    )
    assert root == cache_dir.parent.resolve() / "venvs"
    # Nothing to convert, so nothing is synced: the caller's own sync fills the target.
    assert [argv for argv, _ in uv.calls] == [["uv", "cache", "dir"]]


def test_ensure_recreates_a_target_the_root_filesystem_lost(
    project: Path, cache_dir: Path, root: Path
) -> None:
    uv = _FakeUv(cache_dir)
    venv_links.ensure(project, [], run=uv, alive=_never_alive)
    target = venv_links.target_for(root, project)
    venv_links.remove_target(target)
    assert (project / ".venv").is_symlink() and not (project / ".venv").exists()

    venv_links.ensure(project, [], run=uv, alive=_never_alive)

    assert target.is_dir()
    assert venv_links.read_owner(target) is not None


def test_ensure_leaves_a_link_that_already_resolves(
    project: Path, cache_dir: Path, root: Path
) -> None:
    uv = _FakeUv(cache_dir)
    elsewhere = root / "made-by-the-apply"
    venv_links.write_owner(elsewhere, project / ".venv")
    elsewhere.mkdir()
    (elsewhere / "bin").mkdir()
    venv_links.switch_link(project, elsewhere)

    venv_links.ensure(project, [], run=uv, alive=_never_alive)

    assert os.path.realpath(project / ".venv") == os.path.realpath(elsewhere)
    assert (elsewhere / "bin").is_dir()


def test_ensure_converts_a_real_venv_into_a_synced_linked_one(
    project: Path, cache_dir: Path, root: Path
) -> None:
    (project / ".venv" / "lib").mkdir(parents=True)
    uv = _FakeUv(cache_dir)

    venv_links.ensure(project, [], run=uv, alive=_never_alive)

    target = venv_links.target_for(root, project)
    assert (project / ".venv").is_symlink()
    assert (target / "bin" / "python").is_file()
    assert not (project / venv_links.CONVERTING_NAME).exists()
    # The sync ran against the new target, the root workspace's way.
    assert uv.venv_during_sync == [Path(os.path.realpath(target))]
    assert ["uv", "sync", "--all-packages", "--frozen"] in [argv for argv, _ in uv.calls]


def test_a_conversion_whose_sync_fails_puts_the_real_venv_back(
    project: Path, cache_dir: Path, root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project / ".venv").mkdir()
    (project / ".venv" / "kept.txt").write_text("still here")
    uv = _FakeUv(cache_dir, sync_fails=True)

    venv_links.ensure(project, [], run=uv, alive=_never_alive)

    venv = project / ".venv"
    assert not venv.is_symlink() and (venv / "kept.txt").read_text() == "still here"
    assert not (project / venv_links.CONVERTING_NAME).exists()
    assert list(root.iterdir()) == []
    assert "error: no network" in capsys.readouterr().err


def test_an_interrupted_conversion_is_undone_and_redone(
    project: Path, cache_dir: Path, root: Path
) -> None:
    # The state a kill between moving the real venv aside and finishing its sync leaves.
    aside = project / venv_links.CONVERTING_NAME
    (aside / "lib").mkdir(parents=True)
    half = venv_links.target_for(root, project)
    venv_links.write_owner(half, project / ".venv")
    half.mkdir(parents=True)
    venv_links.switch_link(project, half)
    uv = _FakeUv(cache_dir)

    venv_links.ensure(project, [], run=uv, alive=_never_alive)

    assert (project / ".venv").is_symlink()
    assert (half / "bin" / "python").is_file()
    assert not aside.exists()


def test_a_standalone_project_converts_with_its_own_locked_sync(
    project: Path, cache_dir: Path
) -> None:
    package = project / "system" / "apps" / "widget"
    (package / ".venv").mkdir(parents=True)
    uv = _FakeUv(cache_dir)

    venv_links.ensure(project, [package], run=uv, alive=_never_alive)

    assert (package / ".venv").is_symlink()
    assert (["uv", "sync", "--locked"], package) in uv.calls


def _target(root: Path, name: str, link: Path, holder_pid: int | None = None) -> Path:
    target = root / name
    venv_links.write_owner(target, link, holder_pid)
    target.mkdir(parents=True)
    return target


def test_sweep_keeps_what_a_link_serves_and_removes_the_rest(
    tmp_path: Path, root: Path
) -> None:
    live = tmp_path / "live"
    live.mkdir()
    served = _target(root, "served", live / ".venv")
    venv_links.switch_link(live, served)

    gone = _target(root, "link-gone", tmp_path / "removed-worktree" / ".venv")

    moved = tmp_path / "moved"
    moved.mkdir()
    foreign = _target(root, "foreign", moved / ".venv")
    venv_links.switch_link(moved, served)

    unowned = root / "no-owner"
    unowned.mkdir()

    being_set_up = tmp_path / "being-set-up"
    being_set_up.mkdir()
    pending = root / "pending"
    venv_links.switch_link(being_set_up, pending)
    venv_links.write_owner(pending, being_set_up / ".venv")

    stale_owner = root / "stale"
    venv_links.write_owner(stale_owner, tmp_path / "nowhere" / ".venv")

    removed = venv_links.sweep(root, alive=_never_alive)

    assert served.is_dir()
    assert venv_links.owner_path(pending).is_file()
    assert set(removed) == {
        gone,
        foreign,
        unowned,
        venv_links.owner_path(stale_owner),
    }
    for path in (gone, foreign, unowned):
        assert not path.exists()
        assert not venv_links.owner_path(path).exists()


def test_sweep_keeps_a_target_its_holder_is_still_running_for(
    tmp_path: Path, root: Path
) -> None:
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    held = _target(root, "held", tmp_path / "w" / ".venv", holder_pid=os.getpid())
    abandoned = _target(root, "abandoned", tmp_path / "w" / ".venv", holder_pid=process.pid)

    removed = venv_links.sweep(root)

    assert held.is_dir()
    assert removed == [abandoned]


def test_fresh_switch_and_remove_swap_a_venv_without_touching_the_old_target(
    project: Path, cache_dir: Path, root: Path
) -> None:
    venv_links.ensure(project, [], run=_FakeUv(cache_dir), alive=_never_alive)
    old = venv_links.target_for(root, project)

    new = venv_links.fresh_target(root, project, holder_pid=os.getpid())

    assert new.is_dir() and new != old
    assert venv_links.read_owner(new) == venv_links.Owner(
        link=str(project / ".venv"), holder_pid=os.getpid()
    )
    # Held, so a sweep that runs before the switch keeps it.
    assert venv_links.sweep(root) == []

    venv_links.switch_link(project, new)
    assert os.path.realpath(project / ".venv") == os.path.realpath(new)
    venv_links.hold(old, os.getpid())
    assert venv_links.sweep(root) == []

    venv_links.remove_target(old)
    assert not old.exists() and not venv_links.owner_path(old).exists()
    venv_links.hold(new, None)
    assert venv_links.sweep(root) == []


def test_switch_link_refuses_a_real_directory(project: Path, root: Path) -> None:
    (project / ".venv").mkdir()
    with pytest.raises(IsADirectoryError):
        venv_links.switch_link(project, root / "anything")


def test_the_filesystem_check_warns_naming_both_paths(
    root: Path, cache_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert venv_links.check_same_filesystem(root, cache_dir) is True
    assert capsys.readouterr().err == ""

    # /dev is its own filesystem on both Linux and macOS.
    other = Path("/dev")
    assert os.stat(other).st_dev != os.stat(root).st_dev

    assert venv_links.check_same_filesystem(root, other) is False
    err = capsys.readouterr().err
    assert str(root) in err and str(other) in err


def test_target_keys_are_readable_and_distinct(root: Path) -> None:
    a = venv_links.target_for(root, Path("/home/user/workspace"))
    b = venv_links.target_for(root, Path("/home/user/worktrees/fix-thing"))

    assert a.name.startswith("home-user-workspace-")
    assert b.name.startswith("home-user-worktrees-fix-thing-")
    assert a != b
    long = venv_links.target_for(root, Path("/x" * 80 + "/project"))
    assert len(long.name) <= 60 + 1 + 10 and long.name.split("-")[-2] == "project"

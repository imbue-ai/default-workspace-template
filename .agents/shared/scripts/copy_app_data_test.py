"""Tests for ``copy_app_data.py``.

Run via: ``uv run pytest .agents/shared/scripts/copy_app_data_test.py``

The free-space probe is injected, so a copy's fit is decided by the test rather
than by whatever disk the test runs on.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parent / "copy_app_data.py"
_spec = importlib.util.spec_from_file_location("copy_app_data", _SCRIPT)
assert _spec is not None and _spec.loader is not None
mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = mod
_spec.loader.exec_module(mod)

_APP = "pr-review"


def _plenty(_path: Path) -> int:
    return 1024**4


def _seed_app_data(repo_root: Path) -> Path:
    source = repo_root / mod.APPS_DATA_ROOT / _APP
    (source / "repos" / "one").mkdir(parents=True)
    (source / "repos" / "one" / "pack.bin").write_bytes(b"x" * 3000)
    (source / "records.json").write_text("[1]")
    os.symlink("records.json", source / "latest.json")
    return source


def test_tree_size_counts_file_bytes_and_not_symlink_targets(tmp_path: Path) -> None:
    source = _seed_app_data(tmp_path)
    assert mod.tree_size_bytes(source) == 3000 + len("[1]")


def test_a_copy_that_fits_is_made_whole_with_symlinks_kept(tmp_path: Path) -> None:
    source = _seed_app_data(tmp_path)
    destination = tmp_path / "copies" / "data"

    mod.copy_tree_checked(source, destination, free_space=_plenty)

    assert (destination / "repos" / "one" / "pack.bin").read_bytes() == b"x" * 3000
    assert os.readlink(destination / "latest.json") == "records.json"


def test_a_copy_that_would_not_leave_the_reserve_is_refused_before_writing(
    tmp_path: Path,
) -> None:
    source = _seed_app_data(tmp_path)
    destination = tmp_path / "copies" / "data"
    probed: list[Path] = []

    def just_short(path: Path) -> int:
        probed.append(path)
        return mod.RESERVE_BYTES + mod.tree_size_bytes(source) - 1

    with pytest.raises(mod.CopyError, match="/tmp is memory"):
        mod.copy_tree_checked(source, destination, free_space=just_short)

    assert not destination.parent.exists()
    # The probe asks about the disk the copy would land on, not the source's.
    assert probed == [tmp_path]


def test_a_copy_that_fails_part_way_is_removed(tmp_path: Path) -> None:
    # copytree copies everything else, then fails on the pipe it cannot copy.
    source = _seed_app_data(tmp_path)
    os.mkfifo(source / "repos" / "one" / "pipe")
    destination = tmp_path / "copies" / "data"

    with pytest.raises(mod.CopyError, match="failed"):
        mod.copy_tree_checked(source, destination, free_space=_plenty)

    assert not destination.exists()


def test_a_copy_never_lands_on_an_existing_destination(tmp_path: Path) -> None:
    source = _seed_app_data(tmp_path)
    destination = tmp_path / "copies" / "data"
    destination.mkdir(parents=True)
    (destination / "kept.json").write_text("{}")

    with pytest.raises(mod.CopyError, match="already exists"):
        mod.copy_tree_checked(source, destination, free_space=_plenty)

    assert (destination / "kept.json").exists()


def test_snapshot_then_drop_round_trip(tmp_path: Path) -> None:
    _seed_app_data(tmp_path)

    path = mod.snapshot(tmp_path, _APP, "pre-v2", free_space=_plenty)

    assert path == tmp_path / mod.SNAPSHOT_ROOT / _APP / "pre-v2"
    assert (path / "records.json").read_text() == "[1]"
    assert mod.drop(tmp_path, _APP, "pre-v2") is True
    assert not (tmp_path / mod.SNAPSHOT_ROOT / _APP).exists()
    assert mod.drop(tmp_path, _APP, "pre-v2") is False


def test_drop_keeps_the_apps_other_snapshots(tmp_path: Path) -> None:
    _seed_app_data(tmp_path)
    mod.snapshot(tmp_path, _APP, "pre-a", free_space=_plenty)
    kept = mod.snapshot(tmp_path, _APP, "pre-b", free_space=_plenty)

    mod.drop(tmp_path, _APP, "pre-a")

    assert (kept / "records.json").exists()


def test_snapshot_never_overwrites_an_existing_one(tmp_path: Path) -> None:
    source = _seed_app_data(tmp_path)
    path = mod.snapshot(tmp_path, _APP, "pre-v2", free_space=_plenty)
    (source / "records.json").write_text("[1, 2]")

    with pytest.raises(mod.CopyError, match="already exists"):
        mod.snapshot(tmp_path, _APP, "pre-v2", free_space=_plenty)

    assert (path / "records.json").read_text() == "[1]"


@pytest.mark.parametrize(
    ("app", "label"), [("../chat", "x"), (_APP, "a/b"), (_APP, "..")]
)
def test_names_that_would_escape_the_snapshot_root_are_refused(
    tmp_path: Path, app: str, label: str
) -> None:
    _seed_app_data(tmp_path)
    with pytest.raises(mod.CopyError, match="plain name"):
        mod.snapshot(tmp_path, app, label, free_space=_plenty)
    with pytest.raises(mod.CopyError, match="plain name"):
        mod.drop(tmp_path, app, label)


def test_main_snapshot_prints_the_path_and_fails_for_an_app_with_no_data(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_app_data(tmp_path)
    root = ["--repo-root", str(tmp_path)]

    assert mod.main(["snapshot", "--app", _APP, "--label", "pre-v2", *root]) == 0
    printed = Path(capsys.readouterr().out.strip())
    assert (printed / "records.json").read_text() == "[1]"

    assert mod.main(["snapshot", "--app", "never-ran", "--label", "pre-v2", *root]) == 1
    assert "no data to snapshot" in capsys.readouterr().err

    assert mod.main(["drop", "--app", _APP, "--label", "pre-v2", *root]) == 0
    assert not printed.exists()

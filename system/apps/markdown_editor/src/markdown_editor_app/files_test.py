import hashlib
import os
import threading
import time
from pathlib import Path

import pytest

from markdown_editor_app import files as files_module
from markdown_editor_app.errors import FileRefusedError
from markdown_editor_app.files import MarkdownFinder
from markdown_editor_app.files import WorkspaceFiles
from markdown_editor_app.files import scan_markdown


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _refused_status(files: WorkspaceFiles, path: str) -> int:
    with pytest.raises(FileRefusedError) as refusal:
        files.read(path)
    return refusal.value.status


def test_a_markdown_file_opens_with_its_version_and_is_writable(files: WorkspaceFiles, workspace: Path) -> None:
    plan = workspace / "plan.md"
    plan.write_text("# Plan\n\nSteps.\n")
    record = files.read(str(plan))
    assert record.text == "# Plan\n\nSteps.\n"
    assert record.sha256 == _sha(record.text)
    assert record.is_writable is True
    assert record.name == "plan.md"


def test_a_relative_path_names_a_file_under_the_workspace(files: WorkspaceFiles, workspace: Path) -> None:
    (workspace / "notes").mkdir()
    (workspace / "notes" / "a.md").write_text("a\n")
    assert files.read("notes/a.md").path == str((workspace / "notes" / "a.md").resolve())


def test_a_path_outside_the_home_is_refused_however_it_is_spelled(
    files: WorkspaceFiles, workspace: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("secret\n")
    assert _refused_status(files, str(outside)) == 403
    assert _refused_status(files, str(workspace / ".." / ".." / "outside.md")) == 403


def test_a_link_that_leaves_the_home_is_refused(files: WorkspaceFiles, workspace: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("secret\n")
    (workspace / "link.md").symlink_to(outside)
    assert _refused_status(files, str(workspace / "link.md")) == 403


def test_a_credentials_folder_is_never_opened(files: WorkspaceFiles, workspace: Path) -> None:
    secrets = workspace / "data" / ".secrets"
    secrets.mkdir(parents=True)
    (secrets / "notes.md").write_text("x\n")
    assert _refused_status(files, str(secrets / "notes.md")) == 403


def test_an_empty_or_missing_path_is_refused(files: WorkspaceFiles, workspace: Path) -> None:
    assert _refused_status(files, "") == 400
    assert _refused_status(files, str(workspace / "nope.md")) == 404


def test_another_kind_of_file_opens_read_only_and_is_never_saved(files: WorkspaceFiles, workspace: Path) -> None:
    script = workspace / "run.py"
    script.write_text("print(1)\n")
    assert files.read(str(script)).is_writable is False
    with pytest.raises(FileRefusedError) as refusal:
        files.write(str(script), "print(2)\n", None)
    assert refusal.value.status == 403
    assert script.read_text() == "print(1)\n"


def test_a_binary_file_is_refused(files: WorkspaceFiles, workspace: Path) -> None:
    blob = workspace / "image.md"
    blob.write_bytes(b"\xff\xfe\x00binary")
    assert _refused_status(files, str(blob)) == 415


def test_a_save_checks_its_base_version_keeps_the_mode_and_the_replaced_version(
    files: WorkspaceFiles, workspace: Path, history_dir: Path
) -> None:
    plan = workspace / "plan.md"
    plan.write_text("one\n")
    os.chmod(plan, 0o640)

    saved = files.write(str(plan), "two\n", _sha("one\n"))

    assert plan.read_text() == "two\n"
    assert saved.sha256 == _sha("two\n")
    assert plan.stat().st_mode & 0o777 == 0o640
    folder = files.history_folder(plan.resolve())
    assert (folder / "source.txt").read_text() == f"{plan.resolve()}\n"
    assert [item.read_text() for item in folder.iterdir() if item.name != "source.txt"] == ["one\n"]
    assert [item.name for item in workspace.iterdir()] == ["plan.md"]

    with pytest.raises(FileRefusedError) as refusal:
        files.write(str(plan), "three\n", _sha("one\n"))
    assert refusal.value.status == 409
    assert plan.read_text() == "two\n"

    files.write(str(plan), "three\n", None)
    assert plan.read_text() == "three\n"


def test_saving_the_same_text_writes_nothing(files: WorkspaceFiles, workspace: Path, history_dir: Path) -> None:
    plan = workspace / "plan.md"
    plan.write_text("same\n")
    files.write(str(plan), "same\n", _sha("same\n"))
    assert not history_dir.exists()


def test_the_history_keeps_only_the_newest_versions(
    files: WorkspaceFiles, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(files_module, "HISTORY_KEEP", 3)
    plan = workspace / "plan.md"
    plan.write_text("v0\n")
    for number in range(1, 6):
        files.write(str(plan), f"v{number}\n", None)
    folder = files.history_folder(plan.resolve())
    kept = sorted(item.read_text() for item in folder.iterdir() if item.name != "source.txt")
    assert kept == ["v2\n", "v3\n", "v4\n"]


def test_markdown_is_listed_newest_first_skipping_machine_and_hidden_folders(
    files: WorkspaceFiles, workspace: Path
) -> None:
    (workspace / "old.md").write_text("old\n")
    os.utime(workspace / "old.md", (1_000_000, 1_000_000))
    (workspace / "docs").mkdir()
    (workspace / "docs" / "new.md").write_text("new\n")
    (workspace / "node_modules" / "pkg").mkdir(parents=True)
    (workspace / "node_modules" / "pkg" / "README.md").write_text("skip\n")
    (workspace / ".tickets").mkdir()
    (workspace / ".tickets" / "t-1.md").write_text("ticket\n")
    (workspace / "notes.txt").write_text("not markdown\n")
    assert [item.name for item in scan_markdown(files, workspace)] == ["new.md", "old.md"]


def test_paths_are_shown_under_the_homes_own_name(workspace: Path, history_dir: Path, tmp_path: Path) -> None:
    # On Studio /home/user is a link to the volume; files are shown under /home/user, not the volume's path.
    named_home = tmp_path / "named-home"
    named_home.symlink_to(workspace.parent)
    files = WorkspaceFiles(root=named_home, search_root=workspace, history_dir=history_dir)
    (workspace / "plan.md").write_text("x\n")
    shown = str(named_home / "workspace" / "plan.md")
    assert files.read(shown).path == shown
    assert [item.path for item in scan_markdown(files, workspace)] == [shown]


def test_a_stale_list_is_answered_at_once_and_refreshed_behind_it(files: WorkspaceFiles, workspace: Path) -> None:
    (workspace / "first.md").write_text("1\n")
    finder = MarkdownFinder(files, cache_seconds=0.0)
    assert [item.name for item in finder.find()] == ["first.md"]
    (workspace / "second.md").write_text("2\n")

    assert [item.name for item in finder.find()] == ["first.md"]

    pause = threading.Event()
    deadline = time.monotonic() + 5
    while finder.is_refreshing and time.monotonic() < deadline:
        pause.wait(0.05)
    assert sorted(item.name for item in finder.find()) == ["first.md", "second.md"]


def test_a_fresh_list_is_answered_from_the_cache(files: WorkspaceFiles, workspace: Path) -> None:
    (workspace / "first.md").write_text("1\n")
    finder = MarkdownFinder(files)
    assert [item.name for item in finder.find()] == ["first.md"]
    (workspace / "second.md").write_text("2\n")
    assert [item.name for item in finder.find()] == ["first.md"]
    assert not finder.is_refreshing

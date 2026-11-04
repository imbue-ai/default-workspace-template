"""Unit tests for the chat-attachment storage helpers."""

import io
import re
import struct
from pathlib import Path

import pytest
from werkzeug.datastructures import FileStorage

from imbue.chat.attachments import _DEFAULT_UPLOAD_FILENAME
from imbue.chat.attachments import _move_to_sized_subdirectory
from imbue.chat.attachments import delete_upload
from imbue.chat.attachments import get_uploads_directory
from imbue.chat.attachments import resolve_upload_path
from imbue.chat.attachments import sanitize_upload_filename
from imbue.chat.attachments import store_uploaded_file


def _file_storage(content: bytes, filename: str) -> FileStorage:
    return FileStorage(stream=io.BytesIO(content), filename=filename)


def test_get_uploads_directory_is_under_agent_work_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", str(tmp_path))

    uploads_directory = get_uploads_directory()

    assert uploads_directory == tmp_path / "data" / "uploads"


def test_sanitize_upload_filename_keeps_safe_names() -> None:
    assert sanitize_upload_filename("diagram.png") == "diagram.png"


def test_sanitize_upload_filename_strips_path_separators() -> None:
    sanitized = sanitize_upload_filename("../../etc/passwd")

    assert "/" not in sanitized
    assert ".." not in sanitized


def test_sanitize_upload_filename_falls_back_when_empty() -> None:
    assert sanitize_upload_filename("???") == _DEFAULT_UPLOAD_FILENAME


def test_store_uploaded_file_writes_content_under_unique_subdir(tmp_path: Path) -> None:
    uploads_directory = tmp_path / "uploads"

    stored_path = store_uploaded_file(uploads_directory, "photo.png", _file_storage(b"image-bytes", "photo.png"))

    assert stored_path.read_bytes() == b"image-bytes"
    assert stored_path.name == "photo.png"
    assert stored_path.parent.parent == uploads_directory


_PNG_1600X900 = (
    b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", 1600, 900) + b"\x08\x06\x00\x00\x00"
)


def test_an_image_upload_names_its_subdirectory_after_the_size_it_is_drawn_at(tmp_path: Path) -> None:
    uploads_directory = tmp_path / "uploads"
    png = _PNG_1600X900

    stored_path = store_uploaded_file(uploads_directory, "photo.png", _file_storage(png, "photo.png"))

    assert re.fullmatch(r"[0-9a-f]{32}-1600x900", stored_path.parent.name)
    assert stored_path.read_bytes() == png
    assert [path.name for path in uploads_directory.iterdir()] == [stored_path.parent.name]
    relative_path = f"{stored_path.parent.name}/{stored_path.name}"
    assert resolve_upload_path(uploads_directory, relative_path) == stored_path.resolve()


def test_an_image_whose_subdirectory_cannot_be_renamed_is_kept_without_its_size(tmp_path: Path) -> None:
    unique_subdirectory = tmp_path / "uploads" / "0123456789abcdef0123456789abcdef"
    unique_subdirectory.mkdir(parents=True)
    (unique_subdirectory / "photo.png").write_bytes(_PNG_1600X900)
    # A directory cannot be renamed onto one that is not empty.
    sized_subdirectory = tmp_path / "uploads" / f"{unique_subdirectory.name}-1600x900"
    sized_subdirectory.mkdir()
    (sized_subdirectory / "occupant").write_bytes(b"")

    stored_path = _move_to_sized_subdirectory(unique_subdirectory, sized_subdirectory, "photo.png")

    assert stored_path == unique_subdirectory / "photo.png"
    assert stored_path.read_bytes() == _PNG_1600X900


def test_a_file_with_no_readable_size_keeps_a_plain_subdirectory(tmp_path: Path) -> None:
    uploads_directory = tmp_path / "uploads"

    stored_path = store_uploaded_file(uploads_directory, "report.pdf", _file_storage(b"%PDF-1.7", "report.pdf"))

    assert re.fullmatch(r"[0-9a-f]{32}", stored_path.parent.name)


def test_store_uploaded_file_keeps_same_named_files_separate(tmp_path: Path) -> None:
    uploads_directory = tmp_path / "uploads"

    first_path = store_uploaded_file(uploads_directory, "report.pdf", _file_storage(b"first", "report.pdf"))
    second_path = store_uploaded_file(uploads_directory, "report.pdf", _file_storage(b"second", "report.pdf"))

    assert first_path != second_path
    assert first_path.read_bytes() == b"first"
    assert second_path.read_bytes() == b"second"


def test_resolve_upload_path_returns_path_for_stored_file(tmp_path: Path) -> None:
    uploads_directory = tmp_path / "uploads"
    stored_path = store_uploaded_file(uploads_directory, "note.txt", _file_storage(b"hi", "note.txt"))
    relative_path = f"{stored_path.parent.name}/{stored_path.name}"

    resolved = resolve_upload_path(uploads_directory, relative_path)

    assert resolved == stored_path.resolve()


def test_resolve_upload_path_rejects_traversal(tmp_path: Path) -> None:
    uploads_directory = tmp_path / "uploads"
    uploads_directory.mkdir(parents=True)
    secret = tmp_path / "secret.txt"
    secret.write_text("classified")

    assert resolve_upload_path(uploads_directory, "../secret.txt") is None


def test_resolve_upload_path_returns_none_for_missing_file(tmp_path: Path) -> None:
    uploads_directory = tmp_path / "uploads"
    uploads_directory.mkdir(parents=True)

    assert resolve_upload_path(uploads_directory, "nope/missing.png") is None


def test_delete_upload_removes_file_and_subdir(tmp_path: Path) -> None:
    uploads_directory = tmp_path / "uploads"
    stored_path = store_uploaded_file(uploads_directory, "gone.txt", _file_storage(b"bye", "gone.txt"))
    relative_path = f"{stored_path.parent.name}/{stored_path.name}"

    delete_upload(uploads_directory, relative_path)

    assert not stored_path.exists()
    assert not stored_path.parent.exists()


def test_delete_upload_is_idempotent(tmp_path: Path) -> None:
    uploads_directory = tmp_path / "uploads"
    stored_path = store_uploaded_file(uploads_directory, "gone.txt", _file_storage(b"bye", "gone.txt"))
    relative_path = f"{stored_path.parent.name}/{stored_path.name}"

    delete_upload(uploads_directory, relative_path)
    delete_upload(uploads_directory, relative_path)

    assert not stored_path.exists()


def test_delete_upload_ignores_traversal(tmp_path: Path) -> None:
    uploads_directory = tmp_path / "uploads"
    uploads_directory.mkdir(parents=True)
    secret = tmp_path / "secret.txt"
    secret.write_text("classified")

    delete_upload(uploads_directory, "../secret.txt")

    assert secret.exists()

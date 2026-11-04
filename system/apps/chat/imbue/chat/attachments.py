import os
import uuid
from pathlib import Path

from loguru import logger
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from imbue.chat.image_sizes import read_image_size
from imbue.chat.models import AttachmentError

# Chat attachments live under the gitignored ``data/uploads/`` directory,
# beside the rest of the user's workspace data. A user upload can be
# arbitrarily large and of any format, so uploads are never committed to git;
# they ride the restic host backup with the rest of ``data/``.
_UPLOADS_SUBPATH = Path("data/uploads")

_DEFAULT_UPLOAD_FILENAME = "upload"


def get_uploads_directory() -> Path:
    """Return the directory where chat attachments are stored on the agent VM.

    Resolved under the primary agent's work dir (the workspace repo root, where
    ``data/`` lives), falling back to the current working directory when
    ``MNGR_AGENT_WORK_DIR`` is unset -- mirroring how the rest of the app
    resolves workspace-data paths.
    """
    work_dir = os.environ.get("MNGR_AGENT_WORK_DIR")
    base_directory = Path(work_dir) if work_dir else Path.cwd()
    return base_directory / _UPLOADS_SUBPATH


def sanitize_upload_filename(original_filename: str) -> str:
    """Return a filesystem-safe basename for a user-provided filename."""
    safe_name = secure_filename(original_filename)
    if not safe_name:
        return _DEFAULT_UPLOAD_FILENAME
    return safe_name


def store_uploaded_file(uploads_directory: Path, original_filename: str, file_storage: FileStorage) -> Path:
    """Store an uploaded file under a fresh unique subdirectory and return its path.

    Each upload gets its own subdirectory so two files with the same name never
    collide. An image's subdirectory also carries the size it is drawn at
    (``<id>-<width>x<height>``), so a chat bubble naming it can hold its space
    before it loads (``uploadedImageSize`` in frontend/src/models/attachments.ts
    reads it back). Raises AttachmentError if the file cannot be written.
    """
    unique_id = uuid.uuid4().hex
    safe_name = sanitize_upload_filename(original_filename)
    unique_subdirectory = uploads_directory / unique_id
    destination_path = unique_subdirectory / safe_name
    try:
        unique_subdirectory.mkdir(parents=True, exist_ok=True)
        file_storage.save(destination_path)
    except OSError as e:
        raise AttachmentError(f"Could not store uploaded file '{original_filename}'") from e
    size = read_image_size(destination_path)
    if size is None:
        return destination_path
    sized_subdirectory = uploads_directory / f"{unique_id}-{size.width}x{size.height}"
    return _move_to_sized_subdirectory(unique_subdirectory, sized_subdirectory, safe_name)


def _move_to_sized_subdirectory(unique_subdirectory: Path, sized_subdirectory: Path, safe_name: str) -> Path:
    """Rename an upload's subdirectory to the one naming its size and return the file's new path.

    The size is only a hint for drawing the image, so when the rename fails the file stays where it
    was saved and its unsized path is returned.
    """
    try:
        unique_subdirectory.rename(sized_subdirectory)
    except OSError as e:
        logger.warning("Kept upload {} without its size in the path: {}", unique_subdirectory / safe_name, e)
        return unique_subdirectory / safe_name
    return sized_subdirectory / safe_name


def resolve_upload_path(uploads_directory: Path, relative_path: str) -> Path | None:
    """Resolve a client-supplied relative path to a file inside the uploads dir.

    Returns the resolved path only when it stays within the uploads directory
    and points at an existing file; returns None for traversal attempts or
    misses.
    """
    base_directory = uploads_directory.resolve()
    candidate_path = (uploads_directory / relative_path).resolve()
    if not candidate_path.is_relative_to(base_directory):
        return None
    if not candidate_path.is_file():
        return None
    return candidate_path


def delete_upload(uploads_directory: Path, relative_path: str) -> None:
    """Delete a stored upload (and its now-empty subdirectory) if present.

    Confined to the uploads directory; a traversal attempt or a miss is a no-op
    so removal is idempotent.
    """
    resolved_path = resolve_upload_path(uploads_directory, relative_path)
    if resolved_path is None:
        return
    try:
        resolved_path.unlink()
        parent_directory = resolved_path.parent
        if parent_directory != uploads_directory.resolve():
            parent_directory.rmdir()
    except OSError as e:
        # Best-effort cleanup: a concurrent delete or a non-empty subdirectory
        # is not worth failing the request over.
        logger.debug("Skipped attachment cleanup for {}: {}", relative_path, e)

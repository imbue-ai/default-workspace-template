"""The workspace's own files, served from a root folder (the filesystem root, as Files runs): listings, bytes, the
changes the frontend makes, and a folder as a zip, streamed as it is written."""

import codecs
import os
import secrets
import shutil
import stat
import urllib.parse
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Final

from flask import Response
from flask import send_file
from loguru import logger
from werkzeug.datastructures import Headers

from files_app.sources import EntryRecord
from files_app.sources import FileData
from files_app.sources import FilesError
from files_app.sources import FolderData
from files_app.sources import FolderStamps
from files_app.sources import PageData

# What a file page offers to edit: small, and text.
EDITABLE_LIMIT: Final[int] = 4 * 1024 * 1024
_TEXT_SNIFF_BYTES: Final[int] = 8 * 1024
_CHUNK: Final[int] = 64 * 1024


class LocalFiles:
    def __init__(self, root: Path) -> None:
        self._root = root

    def _on_disk(self, path: str) -> Path:
        return self._root / path.lstrip("/")

    def _existing(self, path: str) -> Path:
        target = self._on_disk(path)
        if not os.path.lexists(target):
            raise FilesError(404, f"There's nothing at {path}.")
        return target

    def stamps(self, paths: list[str]) -> FolderStamps:
        """What each folder is at now, from one stat: it moves when anything in the folder is added, removed or
        renamed (not when a file in it is only edited). None for a path that is not a folder."""
        return FolderStamps(stamps={path: self._stamp(path) for path in paths})

    def _stamp(self, path: str) -> str | None:
        try:
            status = self._on_disk(path).stat()
        except OSError:
            return None
        if not stat.S_ISDIR(status.st_mode):
            return None
        # The inode too, so a folder replaced by another of the same name reads as changed.
        return f"{status.st_ino}:{status.st_mtime_ns}"

    def describe(self, path: str) -> PageData:
        target = self._existing(path)
        if target.is_dir():
            # Taken before the read, so a change during it moves the stamp past what is listed.
            stamp = self._stamp(path)
            return FolderData(entries=tuple(_entries_of(target)), stamp=stamp)
        return FileData(is_text=_is_text(target))

    def read(self, path: str, headers: Headers) -> Response | None:
        target = self._existing(path)
        if target.is_dir():
            return None
        if not target.is_file():
            raise FilesError(404, f"{path} isn't a file that can be read.")
        # Conditional, so a Range is honoured and an unchanged file is answered 304; always revalidated, so a file
        # changed on disk is never shown from the browser's cache.
        response = send_file(target, conditional=True, max_age=None)
        response.headers["Cache-Control"] = "no-cache"
        return response

    def write(self, path: str, body: Iterator[bytes], length: int | None) -> None:
        target = self._on_disk(path)
        if target.is_dir():
            raise FilesError(409, f"There's already a folder at {path}.")
        # Through a symlink to the file it names, so saving a linked file keeps the link.
        final = Path(os.path.realpath(target))
        final.parent.mkdir(parents=True, exist_ok=True)
        # Written beside the file and swapped in whole, so an upload that is cut short leaves the old file (or none).
        partial = final.parent / f".{final.name}.{secrets.token_hex(4)}.upload"
        kept_mode = stat.S_IMODE(final.stat().st_mode) if final.exists() else None
        is_done = False
        try:
            descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
            # A file written over keeps its mode (an edited script stays executable); a new one takes the umask's.
            if kept_mode is not None:
                os.fchmod(descriptor, kept_mode)
            written = 0
            with os.fdopen(descriptor, "wb") as out:
                for chunk in body:
                    out.write(chunk)
                    written += len(chunk)
            if length is not None and written != length:
                raise FilesError(400, f"The upload of {path} was cut short.")
            os.replace(partial, final)
            is_done = True
        finally:
            if not is_done:
                partial.unlink(missing_ok=True)

    def make_folder(self, path: str) -> None:
        target = self._on_disk(path)
        if os.path.lexists(target):
            raise FilesError(409, f"There's already something at {path}.")
        target.mkdir(parents=True)

    def delete(self, path: str) -> None:
        target = self._existing(path)
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()

    def move(self, path: str, destination: str) -> None:
        source = self._existing(path)
        target = self._on_disk(destination)
        if os.path.lexists(target):
            raise FilesError(412, f"There's already something at {destination}.")
        if source.is_dir() and target.is_relative_to(source):
            raise FilesError(409, "A folder can't be moved into itself.")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(source, target)

    def zip(self, path: str) -> Response:
        folder = self._existing(path)
        if not folder.is_dir():
            raise FilesError(400, f"{path} isn't a folder.")
        name = f"{folder.name or 'Computer'}.zip"
        response = Response(zip_folder(folder), mimetype="application/zip")
        response.headers["Content-Disposition"] = content_disposition("attachment", name)
        return response


def content_disposition(disposition: str, name: str) -> str:
    """The header for ``name``: its plain ``filename`` reduced to ASCII, the exact name kept in ``filename*``."""
    plain = name.encode("ascii", "replace").decode("ascii").replace("?", "_").replace('"', "_")
    return f"{disposition}; filename=\"{plain}\"; filename*=UTF-8''{urllib.parse.quote(name)}"


def _entries_of(folder: Path) -> Iterator[EntryRecord]:
    with os.scandir(folder) as found:
        for entry in found:
            is_symlink = entry.is_symlink()
            try:
                status = entry.stat()
            except OSError:
                # A broken symlink is listed as what it is; anything else that can't be read is left out.
                if not is_symlink:
                    continue
                status = entry.stat(follow_symlinks=False)
            is_folder = stat.S_ISDIR(status.st_mode)
            yield EntryRecord(
                name=entry.name,
                is_folder=is_folder,
                is_symlink=is_symlink,
                size=_count_entries(entry.path) if is_folder else status.st_size,
                mtime=int(status.st_mtime * 1000),
            )


def _count_entries(folder: str) -> int:
    try:
        with os.scandir(folder) as found:
            return sum(1 for _entry in found)
    except OSError:
        return 0


def _is_text(target: Path) -> bool:
    """Small, with no NUL in its first bytes, and decoding as UTF-8 as far as they go."""
    try:
        size = target.stat().st_size
        if size > EDITABLE_LIMIT or not target.is_file():
            return False
        with target.open("rb") as source:
            head = source.read(_TEXT_SNIFF_BYTES)
    except OSError:
        return False
    if b"\x00" in head:
        return False
    try:
        codecs.getincrementaldecoder("utf-8")().decode(head, final=size <= _TEXT_SNIFF_BYTES)
    except UnicodeDecodeError:
        return False
    return True


class _Spool:
    """Where the zip is written: the bytes since the last drain, handed on as the response's next chunk. It cannot
    seek, so ``zipfile`` writes each entry's sizes after its data."""

    def __init__(self) -> None:
        self._chunks: list[bytes] = []

    def write(self, data: bytes) -> int:
        self._chunks.append(bytes(data))
        return len(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass

    def drain(self) -> bytes:
        data = b"".join(self._chunks)
        self._chunks.clear()
        return data


def zip_folder(folder: Path) -> Iterator[bytes]:
    """The folder and everything under it as a zip, in chunks as it is written. Linked folders are not followed, and
    what can't be read (or isn't a regular file, such as a pipe) is left out."""
    spool = _Spool()
    with zipfile.ZipFile(spool, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for current, folders, files in os.walk(folder):
            folders.sort()
            relative = Path(current).relative_to(folder)
            if relative != Path("."):
                archive.writestr(zipfile.ZipInfo(f"{relative.as_posix()}/"), b"")
            for name in sorted(files):
                full = Path(current) / name
                try:
                    if not stat.S_ISREG(full.stat().st_mode):
                        continue
                    source = full.open("rb")
                except OSError as error:
                    logger.debug("Left {} out of a zip: {}", full, error)
                    continue
                info = zipfile.ZipInfo.from_file(full, (relative / name).as_posix(), strict_timestamps=False)
                info.compress_type = zipfile.ZIP_DEFLATED
                with source, archive.open(info, "w", force_zip64=True) as sink:
                    while chunk := source.read(_CHUNK):
                        sink.write(chunk)
                        yield spool.drain()
            yield spool.drain()
    yield spool.drain()

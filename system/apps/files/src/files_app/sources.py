"""What the Files server answers about a path, whether the path is in the workspace or on one of the user's computers:
the page data a folder or a file's page carries (and ``?json`` returns), and the verbs a source of files offers."""

from collections.abc import Iterator
from typing import Literal
from typing import Protocol

from flask import Response
from werkzeug.datastructures import Headers

from imbue.imbue_common.frozen_model import FrozenModel


class FilesError(Exception):
    """A request the source could not carry out, with the status and plain words to answer it with."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class EntryRecord(FrozenModel):
    """One entry of a folder. A folder's ``size`` is how many entries it holds; ``mtime`` is in milliseconds."""

    name: str
    is_folder: bool
    is_symlink: bool
    size: int
    mtime: int


class FolderData(FrozenModel):
    kind: Literal["folder"] = "folder"
    entries: tuple[EntryRecord, ...]


class FileData(FrozenModel):
    kind: Literal["file"] = "file"
    # Whether the file is small and reads as text, so its page offers to edit it.
    is_text: bool


PageData = FolderData | FileData


class FileSource(Protocol):
    """A tree of files the server reaches by path (``/``-rooted, a folder's with its trailing slash or without)."""

    def describe(self, path: str) -> PageData: ...

    def read(self, path: str, headers: Headers) -> Response | None:
        """The file's bytes, or None for a folder, whose page is served instead."""
        ...

    def write(self, path: str, body: Iterator[bytes], length: int | None) -> None: ...

    def make_folder(self, path: str) -> None: ...

    def delete(self, path: str) -> None: ...

    def move(self, path: str, destination: str) -> None: ...

    def zip(self, path: str) -> Response: ...

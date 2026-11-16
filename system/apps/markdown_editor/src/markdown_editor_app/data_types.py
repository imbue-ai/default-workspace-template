from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel


class HealthResponse(FrozenModel):
    """What the health probe answers."""

    status: str = Field(description="Always 'ok'")
    is_frontend_built: bool = Field(description="Whether the page's bundle has been built, so the page can be served")


class FileRecord(FrozenModel):
    """One file as the editor opens it."""

    path: str = Field(description="The file's absolute path, under the name people know it by")
    name: str = Field(description="The file's name")
    text: str = Field(description="The file's whole text")
    sha256: str = Field(description="The hex SHA-256 of the file's bytes: the version a save names as its base")
    size: int = Field(description="The file's size in bytes")
    mtime: float = Field(description="When the file last changed, in seconds since the epoch")
    is_writable: bool = Field(
        description="Whether the editor may save the file (a markdown or text file it can write)"
    )


class FileVersion(FrozenModel):
    """Which version of a file is on disk, which the page polls to notice an outside change."""

    sha256: str = Field(description="The hex SHA-256 of the file's bytes")
    mtime: float = Field(description="When the file last changed, in seconds since the epoch")


class SaveRequest(FrozenModel):
    """A save: the file's new text, and the version the edit started from."""

    path: str = Field(description="The file to save")
    text: str = Field(description="The file's new whole text")
    base_sha256: str | None = Field(
        default=None,
        description="The version the edit started from; the save is refused when the file changed since. "
        "None saves regardless",
    )


class FoundFile(FrozenModel):
    """A markdown file the start page offers."""

    path: str = Field(description="The file's absolute path")
    name: str = Field(description="The file's name")
    size: int = Field(description="The file's size in bytes")
    mtime: float = Field(description="When the file last changed, in seconds since the epoch")


class FoundFiles(FrozenModel):
    """The markdown files in the workspace, most recently changed first."""

    root: str = Field(description="The folder that was searched")
    files: tuple[FoundFile, ...] = Field(description="The files found, newest first, at most 200")

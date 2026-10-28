"""What the workspace's show-around tour has done (the new-onboarding spec, section 3.4): whether it has started, and
the card it resumes at when the agent's reply interrupted it, in ``tour.json`` under the shell's state directory.
Absent or unusable means it has not run and nothing waits to resume in a workspace the bootstrap marked new on its
first boot (``offer_tour`` beside it), so a new workspace offers the tour once; in any other (one that already existed
when an update brought the tour in) it reads as seen."""

from pathlib import Path
from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.system_interface.shell.state_files import STATE_FILES_LOCK
from imbue.system_interface.shell.state_files import parse_versioned_document
from imbue.system_interface.shell.state_files import read_json_object
from imbue.system_interface.shell.state_files import write_json_atomic

TOUR_FILENAME: Final[str] = "tour.json"
# Written by the bootstrap (``OFFER_TOUR_SIGNAL``) on a workspace's first boot.
OFFER_TOUR_FILENAME: Final[str] = "offer_tour"
TOUR_FILE_VERSION: Final[int] = 1
TOUR_ROUTE: Final[str] = "/api/tour"
# Where the tour's pictures are hosted (the new-onboarding spec, section 3.5); the shell's page policy admits
# images from it, since the default admits only the page's own origin.
ONBOARDING_MEDIA_ORIGIN: Final[str] = "https://onboarding-media.imbueminds.com"


class TourProgress(FrozenModel):
    """What the tour has done in this workspace."""

    is_seen: bool = Field(
        description="Whether the tour has started in this workspace, by itself or from the launcher, or is not "
        "offered here (a workspace that existed before the tour)"
    )
    resume_index: int | None = Field(
        default=None,
        ge=0,
        description="The card the tour resumes at (its position in the card list) once the agent is working again, "
        "after its reply interrupted the tour; None when nothing waits to resume",
    )


UNSEEN_TOUR: Final[TourProgress] = TourProgress(is_seen=False, resume_index=None)
UNOFFERED_TOUR: Final[TourProgress] = TourProgress(is_seen=True, resume_index=None)


class _TourDocument(TourProgress):
    """The whole of ``tour.json``: the progress under a format version."""

    version: int = Field(description="The file format version")


class TourStore(MutableModel):
    """Reads and writes ``tour.json`` under the shell's state lock."""

    state_directory: Path = Field(frozen=True, description="The shell's state directory")

    def _path(self) -> Path:
        return self.state_directory / TOUR_FILENAME

    def read(self) -> TourProgress:
        """What the tour has done; when nothing usable is stored (logged), unseen with nothing to resume in a workspace
        marked new, else seen."""
        with STATE_FILES_LOCK:
            raw = read_json_object(self._path())
        document = parse_versioned_document(raw, _TourDocument, TOUR_FILE_VERSION, self._path())
        if document is None:
            return UNSEEN_TOUR if (self.state_directory / OFFER_TOUR_FILENAME).exists() else UNOFFERED_TOUR
        return TourProgress(is_seen=document.is_seen, resume_index=document.resume_index)

    def write(self, progress: TourProgress) -> None:
        document = _TourDocument(
            version=TOUR_FILE_VERSION, is_seen=progress.is_seen, resume_index=progress.resume_index
        )
        with STATE_FILES_LOCK:
            write_json_atomic(self._path(), document.model_dump(mode="json"))

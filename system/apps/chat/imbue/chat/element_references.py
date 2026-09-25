"""Reference files: where an element reference too large for a chat's composer is written (the
element-reference-menu plan, section 3.2).

The composer keeps a reference block of at most the block bound as it is; a longer one is
posted here, written whole to a file the agent can read (it runs in this container), and the
composer takes the pointer form the frontend builds from the answered path.
"""

import json
import tempfile
import uuid
from pathlib import Path
from typing import Any
from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel

# The subdirectory of the temporary directory the files go in.
ELEMENT_REFERENCES_SUBDIRECTORY: Final[str] = "element_references"


class ElementReferenceEnvelope(FrozenModel):
    """A reference as it travels: one object under the one ``element_reference`` key, and nothing beside it."""

    element_reference: dict[str, Any] = Field(description="The reference as the page built it")


class ElementReferenceRequest(FrozenModel):
    """The body of ``POST /api/element-references``."""

    reference: ElementReferenceEnvelope = Field(description="The envelope as the page built it")


class ElementReferenceResponse(FrozenModel):
    """The answer: where the reference was written."""

    path: str = Field(description="The reference file's absolute path")


class ElementReferenceWriteError(OSError):
    """The reference file could not be written."""


def get_element_references_directory() -> Path:
    """The directory reference files go in: ``element_references/`` under the system temporary directory."""
    return Path(tempfile.gettempdir()) / ELEMENT_REFERENCES_SUBDIRECTORY


def write_element_reference_file(envelope: ElementReferenceEnvelope, directory: Path) -> Path:
    """Write the envelope, pretty-printed, to a fresh private file under ``directory`` and answer its path.

    Raises ElementReferenceWriteError when the directory or the file cannot be written.
    """
    destination = directory / f"{uuid.uuid4().hex}.json"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(envelope.model_dump(), indent=2) + "\n")
        destination.chmod(0o600)
    except OSError as e:
        raise ElementReferenceWriteError(f"could not write the reference file {destination}") from e
    return destination

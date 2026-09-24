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

from imbue.chat.errors import ChatAppError
from imbue.imbue_common.frozen_model import FrozenModel

# The one key a reference travels under, and the subdirectory of the temporary directory the files go in.
ELEMENT_REFERENCE_KEY: Final[str] = "element_reference"
ELEMENT_REFERENCES_SUBDIRECTORY: Final[str] = "element_references"


class ElementReferenceRequest(FrozenModel):
    """The body of ``POST /api/element-references``: the envelope as the page built it."""

    reference: dict[str, Any] = Field(description="The envelope: one object under the ``element_reference`` key")


class ElementReferenceResponse(FrozenModel):
    """The answer: where the reference was written."""

    path: str = Field(description="The reference file's absolute path")


class ElementReferenceError(ChatAppError):
    """The posted object is not a reference envelope, or the file could not be written."""


def get_element_references_directory() -> Path:
    """The directory reference files go in: ``element_references/`` under the system temporary directory."""
    return Path(tempfile.gettempdir()) / ELEMENT_REFERENCES_SUBDIRECTORY


def validate_reference_envelope(reference: dict[str, Any]) -> dict[str, Any]:
    """The envelope when it is one object under the one key; anything else raises ElementReferenceError."""
    if set(reference) != {ELEMENT_REFERENCE_KEY}:
        raise ElementReferenceError(f"the reference must be one object under the {ELEMENT_REFERENCE_KEY!r} key")
    inner = reference[ELEMENT_REFERENCE_KEY]
    if not isinstance(inner, dict):
        raise ElementReferenceError(f"{ELEMENT_REFERENCE_KEY!r} must hold an object")
    return reference


def write_element_reference_file(reference: dict[str, Any], directory: Path) -> Path:
    """Write the envelope, pretty-printed, to a fresh file under ``directory`` and answer its path.

    Raises ElementReferenceError when the directory or the file cannot be written.
    """
    envelope = validate_reference_envelope(reference)
    destination = directory / f"{uuid.uuid4().hex}.json"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(envelope, indent=2) + "\n")
        destination.chmod(0o600)
    except OSError as e:
        raise ElementReferenceError(f"could not write the reference file {destination}") from e
    return destination

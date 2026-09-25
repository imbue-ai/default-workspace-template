import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from imbue.chat.element_references import ELEMENT_REFERENCES_SUBDIRECTORY
from imbue.chat.element_references import ElementReferenceEnvelope
from imbue.chat.element_references import ElementReferenceWriteError
from imbue.chat.element_references import get_element_references_directory
from imbue.chat.element_references import write_element_reference_file


def _envelope() -> ElementReferenceEnvelope:
    return ElementReferenceEnvelope(
        element_reference={"app": "chat", "tag": "a", "text": "Read the intro", "ancestors": []}
    )


def test_a_reference_is_written_whole_to_a_fresh_private_file(tmp_path: Path) -> None:
    directory = tmp_path / "refs"
    written = write_element_reference_file(_envelope(), directory)
    assert written.parent == directory
    assert written.suffix == ".json"
    assert json.loads(written.read_text()) == _envelope().model_dump()
    assert written.stat().st_mode & 0o777 == 0o600
    second = write_element_reference_file(_envelope(), directory)
    assert second != written


def test_the_directory_is_under_the_temporary_directory() -> None:
    assert get_element_references_directory().name == ELEMENT_REFERENCES_SUBDIRECTORY


@pytest.mark.parametrize(
    "reference",
    [
        {},
        {"element_reference": {}, "extra": 1},
        {"element_reference": "text"},
        {"element_reference_file": "/tmp/x.json"},
    ],
)
def test_anything_but_one_object_under_the_key_is_refused(reference: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ElementReferenceEnvelope.model_validate(reference)


def test_an_unwritable_directory_is_reported(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    with pytest.raises(ElementReferenceWriteError):
        write_element_reference_file(_envelope(), blocker / "refs")

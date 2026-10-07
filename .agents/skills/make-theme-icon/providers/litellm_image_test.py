import base64
from pathlib import Path
from types import SimpleNamespace

import litellm_image
import pytest


def test_an_inline_image_is_written_as_it_came(tmp_path: Path) -> None:
    items = [
        SimpleNamespace(b64_json=base64.b64encode(b"png-bytes").decode(), url=None)
    ]

    paths = litellm_image.write_candidates(items, tmp_path)

    assert [path.name for path in paths] == ["candidate-0.png"]
    assert paths[0].read_bytes() == b"png-bytes"


def test_an_image_with_neither_data_nor_link_is_refused() -> None:
    with pytest.raises(ValueError, match="neither data nor a link"):
        litellm_image.candidate_bytes(SimpleNamespace(b64_json=None, url=None))

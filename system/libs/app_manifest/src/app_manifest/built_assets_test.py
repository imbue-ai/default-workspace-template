from pathlib import Path

import pytest
from flask import Flask
from flask import Response
from werkzeug.exceptions import NotFound

from app_manifest.built_assets import CONTENT_HASHED_MAX_AGE_SECONDS
from app_manifest.built_assets import is_content_hashed
from app_manifest.built_assets import send_built_asset


@pytest.mark.parametrize("name", ["index-DvA3_x-9.js", "index-BScc3eNh.css", "chat-markdown-a1b2C3d4.js"])
def test_a_flat_vite_bundle_name_is_content_hashed(name: str) -> None:
    assert is_content_hashed(name)


@pytest.mark.parametrize(
    "name",
    [
        # Stable names a build copies in under subdirectories, one of which ends in eight name characters.
        "voice/vad-ort/ort-wasm-simd-threaded.js",
        "voice/vad-ort/ort-wasm-simd-threaded.mjs",
        "icons/logo-abcdefgh.css",
        # Not a script or a stylesheet, or no eight-character tail.
        "index-DvA3_x-9.wasm",
        "logo-DvA3_x-9.svg",
        "index-abc.js",
        "index.js",
    ],
)
def test_a_nested_or_unhashed_name_is_not(name: str) -> None:
    assert not is_content_hashed(name)


def _send(directory: Path, name: str) -> Response:
    app = Flask(__name__)
    with app.test_request_context(f"/assets/{name}"):
        return send_built_asset(directory, name)


def test_a_content_hashed_file_is_kept_for_good_and_anything_else_is_revalidated(tmp_path: Path) -> None:
    (tmp_path / "index-DvA3_x-9.js").write_text("export {};")
    (tmp_path / "voice").mkdir()
    (tmp_path / "voice" / "ort-wasm-simd-threaded.js").write_text("export {};")

    hashed = _send(tmp_path, "index-DvA3_x-9.js")
    assert hashed.cache_control.max_age == CONTENT_HASHED_MAX_AGE_SECONDS
    assert hashed.cache_control.immutable
    stable = _send(tmp_path, "voice/ort-wasm-simd-threaded.js")
    assert stable.headers["Cache-Control"] == "no-cache"


def test_a_missing_file_or_one_outside_the_directory_is_not_found(tmp_path: Path) -> None:
    (tmp_path / "assets").mkdir()
    (tmp_path / "secret-DvA3_x-9.js").write_text("export {};")
    with pytest.raises(NotFound):
        _send(tmp_path / "assets", "gone-DvA3_x-9.js")
    with pytest.raises(NotFound):
        _send(tmp_path / "assets", "../secret-DvA3_x-9.js")

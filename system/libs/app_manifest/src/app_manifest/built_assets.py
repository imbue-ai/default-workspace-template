"""How an app serves the files of its frontend's build.

Vite names each bundle file it emits flat in ``assets/`` from its content
(``index-<8 characters>.js``), so the bytes behind such a name never change and
a browser may keep the file for good: it is sent with a year's ``max-age`` and
``immutable``, and a load never asks for it again. Every other file under the
assets route (a stable-named runtime file kept in a subdirectory, such as the
chat's voice models, or anything copied in as is) is sent ``no-cache`` and
revalidated on every load.

Only a flat name is taken as content-hashed: a stable name kept in a
subdirectory can end in eight name characters too (``-threaded``), and a file
wrongly kept for good cannot be taken back short of clearing the site's data.
"""

import re
from pathlib import Path
from typing import Final

from flask import Response
from flask import send_from_directory

CONTENT_HASHED_MAX_AGE_SECONDS: Final[int] = 365 * 24 * 60 * 60

_CONTENT_HASHED_NAME: Final[re.Pattern[str]] = re.compile(r"^[^/]+-[A-Za-z0-9_-]{8}\.(?:js|css)$")


def is_content_hashed(asset_path: str) -> bool:
    """Whether ``asset_path`` (relative to the assets directory) is a bundle file Vite named from its content."""
    return _CONTENT_HASHED_NAME.fullmatch(asset_path) is not None


def send_built_asset(assets_directory: Path, asset_path: str) -> Response:
    """Send one file of the build from ``assets_directory``, cached for good when its name comes from its content
    and revalidated on every load otherwise. Raises ``NotFound`` for a missing file, as ``send_from_directory``
    does, which also keeps ``asset_path`` inside the directory."""
    if not is_content_hashed(asset_path):
        response = send_from_directory(assets_directory, asset_path, max_age=None)
        response.headers["Cache-Control"] = "no-cache"
        return response
    response = send_from_directory(assets_directory, asset_path, max_age=CONTENT_HASHED_MAX_AGE_SECONDS)
    response.cache_control.immutable = True
    return response

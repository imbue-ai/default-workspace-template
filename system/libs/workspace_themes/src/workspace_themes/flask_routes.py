import hashlib
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Final, assert_never

from flask import Flask, Response, abort, request, send_file
from loguru import logger

from workspace_themes.catalog import CachingThemeCatalogLoader
from workspace_themes.contract import BUNDLE_FILE_NAME
from workspace_themes.data_types import ThemeCatalog
from workspace_themes.errors import ThemeFileNotFoundError
from workspace_themes.serving import (
    CSS_MIMETYPE,
    THEME_STATIC_ROUTE_PREFIX,
    ThemeBundle,
    ThemeFile,
    resolve_theme_asset,
    theme_file_security_headers,
)

# Every theme answer is revalidated on each use, so an edited theme shows on the next load (section 5.1).
_CACHE_CONTROL: Final[str] = "no-cache"
_ENDPOINT: Final[str] = "workspace_theme_static"
_ETAG_LENGTH: Final[int] = 16


def _etag_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:_ETAG_LENGTH]


def serve_theme_static(
    load_catalog: Callable[[], ThemeCatalog], asset_path: str
) -> Response:
    """The answer to ``GET /_static/themes/<asset_path>``: the file, or 404."""
    try:
        asset = resolve_theme_asset(load_catalog(), asset_path)
    except ThemeFileNotFoundError as error:
        logger.debug("No theme file for {}: {}", asset_path, error)
        abort(404)
    match asset:
        case ThemeFile(path=path):
            response = send_file(path, conditional=True, max_age=0)
            response.headers.update(theme_file_security_headers(path))
        case ThemeBundle(css=css):
            response = Response(css, mimetype=CSS_MIMETYPE)
            response.headers.update(
                theme_file_security_headers(PurePosixPath(BUNDLE_FILE_NAME))
            )
            # Tagged by its own text: which app overlays it imports follows the apps the workspace has, which the
            # theme's revision does not cover.
            response.set_etag(_etag_of(css))
            response.make_conditional(request)
        case _ as unreachable:
            assert_never(unreachable)
    response.headers["Cache-Control"] = _CACHE_CONTROL
    return response


def register_theme_static_route(
    application: Flask, load_catalog: Callable[[], ThemeCatalog]
) -> None:
    """Serve ``/_static/themes/<path>`` from this app's own origin: the theme bundles and the files they load."""
    application.add_url_rule(
        f"{THEME_STATIC_ROUTE_PREFIX}<path:asset_path>",
        endpoint=_ENDPOINT,
        view_func=lambda asset_path: serve_theme_static(load_catalog, asset_path),
        methods=["GET"],
    )


def register_workspace_theme_route(application: Flask) -> None:
    """Serve the workspace's themes from this app's origin, reading them from the working directory, which is the
    repo root every supervised app runs from."""
    loader = CachingThemeCatalogLoader(repo_root=Path.cwd())
    register_theme_static_route(application, loader.load)

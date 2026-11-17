import hashlib
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Final, assert_never

from app_schema.api_schema import RouteTable, SchemalessRoute
from flask import Blueprint, Flask, Response, abort, request, send_file
from loguru import logger

from workspace_themes.catalog import CachingThemeCatalogLoader
from workspace_themes.contract import (
    BUNDLE_FILE_NAME,
    WORKSPACE_COLOR_ROUTE,
    WORKSPACE_COLOR_STATE_PATH,
)
from workspace_themes.data_types import ThemeCatalog
from workspace_themes.errors import ThemeFileNotFoundError
from workspace_themes.serving import (
    CSS_MIMETYPE,
    THEME_STATIC_ROUTE_PREFIX,
    ThemeBundle,
    ThemeFile,
    ThemeStyleFile,
    resolve_theme_asset,
    theme_file_security_headers,
)
from workspace_themes.workspace_color import read_workspace_color, workspace_color_css

# Every theme answer is revalidated on each use, so an edited theme shows on the next load (section 5.1).
_CACHE_CONTROL: Final[str] = "no-cache"
_ENDPOINT: Final[str] = "workspace_theme_static"
_WORKSPACE_COLOR_ENDPOINT: Final[str] = "workspace_color_static"

# The route-table entry for ``register_theme_static_route``'s route, which every app serving it merges into its own.
THEME_STATIC_ROUTE_TABLE: Final[RouteTable] = {
    ("GET", f"{THEME_STATIC_ROUTE_PREFIX}{{asset_path}}"): SchemalessRoute(
        reason="a workspace theme's bundle, or a file the bundle loads"
    ),
    ("GET", WORKSPACE_COLOR_ROUTE): SchemalessRoute(
        reason="the key colors the workspace color stands for, under the theme bundle"
    ),
}
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
        case ThemeStyleFile(path=path, css=css):
            response = Response(css, mimetype=CSS_MIMETYPE)
            response.headers.update(theme_file_security_headers(path))
            response.set_etag(_etag_of(css))
            response.make_conditional(request)
        case _ as unreachable:
            assert_never(unreachable)
    response.headers["Cache-Control"] = _CACHE_CONTROL
    return response


def register_theme_static_route(
    application: Flask | Blueprint, load_catalog: Callable[[], ThemeCatalog]
) -> None:
    """Serve ``/_static/themes/<path>`` from this app's own origin: the theme bundles and the files they load."""

    def theme_static(asset_path: str) -> Response:
        """A workspace theme's bundle, or a file the bundle loads, served from this app's own origin."""
        return serve_theme_static(load_catalog, asset_path)

    application.add_url_rule(
        f"{THEME_STATIC_ROUTE_PREFIX}<path:asset_path>",
        endpoint=_ENDPOINT,
        view_func=theme_static,
        methods=["GET"],
    )


def serve_workspace_color(state_path: Path) -> Response:
    """The answer to ``GET /_static/workspace_color.css``: the keys the kept workspace color stands for."""
    css = workspace_color_css(read_workspace_color(state_path))
    response = Response(css, mimetype=CSS_MIMETYPE)
    response.headers.update(
        theme_file_security_headers(PurePosixPath(BUNDLE_FILE_NAME))
    )
    response.set_etag(_etag_of(css))
    response.make_conditional(request)
    response.headers["Cache-Control"] = _CACHE_CONTROL
    return response


def register_workspace_color_route(
    application: Flask | Blueprint, repo_root: Path
) -> None:
    """Serve ``/_static/workspace_color.css`` from this app's own origin, from the color the shell keeps."""
    state_path = repo_root / WORKSPACE_COLOR_STATE_PATH

    def workspace_color() -> Response:
        """The key colors the workspace color stands for, which every page loads under its theme."""
        return serve_workspace_color(state_path)

    application.add_url_rule(
        WORKSPACE_COLOR_ROUTE,
        endpoint=_WORKSPACE_COLOR_ENDPOINT,
        view_func=workspace_color,
        methods=["GET"],
    )


def register_workspace_theme_route(application: Flask | Blueprint) -> None:
    """Serve the workspace's themes, and the workspace color's keys, from this app's origin, reading them from the
    working directory, which is the repo root every supervised app runs from."""
    loader = CachingThemeCatalogLoader(repo_root=Path.cwd())
    register_theme_static_route(application, loader.load)
    register_workspace_color_route(application, Path.cwd())

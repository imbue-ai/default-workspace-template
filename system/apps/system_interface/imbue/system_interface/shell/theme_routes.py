"""The shell's theme routes (docs/system/blueprint/workspace-themes/, section 5.2): the catalog of the workspace's
themes with the default it wears, the default's route, each theme's icons, and the theme files every app serves
from its own origin, which the shell serves for its own pages too."""

from flask import Flask
from flask import abort
from flask import jsonify
from flask import send_file
from flask.typing import ResponseReturnValue
from loguru import logger
from pydantic import Field
from workspace_themes.data_types import ThemeCatalog
from workspace_themes.errors import ThemeFileNotFoundError
from workspace_themes.flask_routes import register_theme_static_route
from workspace_themes.primitives import ThemeId
from workspace_themes.serving import resolve_theme_icon
from workspace_themes.serving import theme_file_security_headers

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.system_interface.app_context import get_state
from imbue.system_interface.shell.errors import InvalidShellValueError
from imbue.system_interface.shell.route_helpers import parse_request_body
from imbue.system_interface.shell.state import ShellState
from imbue.system_interface.shell.theme_wire import theme_catalog_wire_json


class ThemeChoiceRequest(FrozenModel):
    """The body of the default theme route and the desktop theme route: a theme id, or null."""

    theme: ThemeId | None = Field(
        description="The theme to wear; null wears the default (or, for the default, standard)"
    )


def _shell() -> ShellState:
    return get_state().shell


def require_available_theme(catalog: ThemeCatalog, theme: ThemeId | None) -> ThemeId | None:
    """The theme id as asked for; raises InvalidShellValueError (a 400) for one that is not available."""
    if theme is not None and catalog.find_available(theme) is None:
        found = catalog.find(theme)
        reason = "there is no such theme" if found is None else "; ".join(found.problems)
        raise InvalidShellValueError(f"theme {str(theme)!r} is not available: {reason}")
    return theme


def list_themes() -> ResponseReturnValue:
    shell = _shell()
    return jsonify(theme_catalog_wire_json(shell.themes.load(), shell.desktops.read_theme_choices()))


def set_default_theme() -> ResponseReturnValue:
    body = parse_request_body(ThemeChoiceRequest)
    shell = _shell()
    catalog = shell.themes.load()
    choices = shell.desktops.set_default_theme(require_available_theme(catalog, body.theme))
    shell.broadcast_themes_changed()
    return jsonify({"default": None if choices.default is None else str(choices.default)})


def serve_theme_icon(theme_id: str, file_name: str) -> ResponseReturnValue:
    try:
        path = resolve_theme_icon(_shell().themes.load(), theme_id, file_name)
    except ThemeFileNotFoundError as error:
        logger.debug("No theme icon {} of {}: {}", file_name, theme_id, error)
        abort(404)
    response = send_file(path, conditional=True, max_age=0)
    response.headers["Cache-Control"] = "no-cache"
    response.headers.update(theme_file_security_headers(path))
    return response


def register_theme_routes(application: Flask) -> None:
    application.add_url_rule("/api/themes", view_func=list_themes, methods=["GET"], endpoint="list_themes")
    application.add_url_rule(
        "/api/themes/default", view_func=set_default_theme, methods=["POST"], endpoint="set_default_theme"
    )
    application.add_url_rule(
        "/api/themes/<theme_id>/icons/<file_name>",
        view_func=serve_theme_icon,
        methods=["GET"],
        endpoint="serve_theme_icon",
    )
    register_theme_static_route(application, lambda: _shell().themes.load())

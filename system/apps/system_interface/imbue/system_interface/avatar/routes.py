"""The avatar routes (pinned-taskbar-entries plan section 5.3): the designs, a design's image and original, and the
workspace's selection; registration is loopback-only, for an agent inside the workspace."""

from typing import Final

from flask import Flask
from flask import Response
from flask import jsonify
from flask import request
from flask.typing import ResponseReturnValue
from loguru import logger

from imbue.system_interface.app_context import get_state
from imbue.system_interface.avatar.catalog import DesignRegistration
from imbue.system_interface.avatar.catalog import design_listing_wire_json
from imbue.system_interface.avatar.designs import AvatarMood
from imbue.system_interface.avatar.designs import DEFAULT_DESIGN_ID
from imbue.system_interface.avatar.designs import MAX_SVG_BYTES
from imbue.system_interface.avatar.designs import render_design_svg
from imbue.system_interface.avatar.icons import DEFAULT_ICON_SIZE
from imbue.system_interface.avatar.icons import render_design_icon_png
from imbue.system_interface.avatar.primitives import DesignId
from imbue.system_interface.avatar.selection import AvatarSelection
from imbue.system_interface.shell.errors import InvalidShellValueError
from imbue.system_interface.shell.route_helpers import HTTP_CREATED
from imbue.system_interface.shell.route_helpers import HTTP_NOT_FOUND
from imbue.system_interface.shell.route_helpers import detail_response
from imbue.system_interface.shell.route_helpers import parse_request_body
from imbue.system_interface.shell.route_helpers import require_loopback
from imbue.system_interface.shell.state import ShellState
from imbue.system_interface.shell.state_files import STATE_FILES_LOCK

# A registration is one design plus its JSON framing; anything larger is not a design.
_MAX_REGISTRATION_BYTES: Final[int] = MAX_SVG_BYTES * 6 + 16384
_SVG_MIMETYPE: Final[str] = "image/svg+xml"
_PNG_MIMETYPE: Final[str] = "image/png"
# The image is isolated by the browser's image mode; the policy also covers a direct visit to the route.
_IMAGE_CSP: Final[str] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
_SOURCE_CSP: Final[str] = "default-src 'none'; sandbox"


def _shell() -> ShellState:
    return get_state().shell


def list_avatars() -> ResponseReturnValue:
    shell = _shell()
    return jsonify(
        {
            "designs": [design_listing_wire_json(listing) for listing in shell.avatar_catalog.entries()],
            "selected": str(shell.avatar_selection.read()),
            "default": str(DEFAULT_DESIGN_ID),
        }
    )


def register_avatar() -> ResponseReturnValue:
    refusal = require_loopback()
    if refusal is not None:
        return refusal
    request.max_content_length = _MAX_REGISTRATION_BYTES
    registration = parse_request_body(DesignRegistration)
    _shell().avatar_catalog.register(registration)
    return jsonify({"id": str(registration.id)}), HTTP_CREATED


def _mood_argument() -> AvatarMood:
    raw = request.args.get("mood", AvatarMood.IDLE.value)
    try:
        return AvatarMood(raw)
    except ValueError:
        raise InvalidShellValueError(
            f"an avatar mood is one of {', '.join(sorted(mood.value for mood in AvatarMood))}, not {raw!r}"
        ) from None


def avatar_image(design_id: str) -> ResponseReturnValue:
    """The design wearing ``mood``; ``preview=1`` holds the pose still."""
    source = _shell().avatar_catalog.source(design_id)
    if source is None:
        return detail_response(f"No avatar design {design_id!r}", HTTP_NOT_FOUND)
    rendered = render_design_svg(source, _mood_argument(), request.args.get("preview") == "1", design_id)
    response = Response(rendered, mimetype=_SVG_MIMETYPE)
    response.headers["Content-Security-Policy"] = _IMAGE_CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-cache"
    return response


def selected_design_id(shell: ShellState) -> DesignId:
    """The design the workspace shows: the selection, or the default when the selected design is no longer in the
    catalog."""
    selected = shell.avatar_selection.read()
    return selected if shell.avatar_catalog.source(selected) is not None else DEFAULT_DESIGN_ID


def icon_png_response(design_id: str, size: int) -> ResponseReturnValue:
    """The design's home-screen icon, ``size`` pixels square; 404 for a design the catalog does not hold."""
    source = _shell().avatar_catalog.source(design_id)
    if source is None:
        return detail_response(f"No avatar design {design_id!r}", HTTP_NOT_FOUND)
    response = Response(render_design_icon_png(source, design_id, size), mimetype=_PNG_MIMETYPE)
    response.headers["X-Content-Type-Options"] = "nosniff"
    # The selection, and a registered design's drawing, can change under one URL.
    response.headers["Cache-Control"] = "no-cache"
    return response


def _size_argument() -> int:
    raw = request.args.get("size", str(DEFAULT_ICON_SIZE))
    try:
        return int(raw)
    except ValueError:
        raise InvalidShellValueError(f"an icon size is a whole number of pixels, not {raw!r}") from None


def avatar_icon(design_id: str) -> ResponseReturnValue:
    """The design's still pose on an opaque square tile, as the PNG a phone's home screen takes."""
    return icon_png_response(design_id, _size_argument())


def avatar_source(design_id: str) -> ResponseReturnValue:
    source = _shell().avatar_catalog.source(design_id)
    if source is None:
        return detail_response(f"No avatar design {design_id!r}", HTTP_NOT_FOUND)
    response = Response(source, mimetype=_SVG_MIMETYPE)
    response.headers["Content-Disposition"] = f'attachment; filename="{design_id}.svg"'
    response.headers["Content-Security-Policy"] = _SOURCE_CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def select_avatar() -> ResponseReturnValue:
    selection = parse_request_body(AvatarSelection)
    shell = _shell()
    if shell.avatar_catalog.source(selection.design) is None:
        raise InvalidShellValueError(f"No avatar design {str(selection.design)!r} to select")
    # Under the state lock so two choices at once are announced in the order they were written.
    with STATE_FILES_LOCK:
        shell.avatar_selection.write(selection.design)
        shell.broadcaster.broadcast_avatar_selection_changed(str(selection.design))
    logger.info("Selected avatar design {}", selection.design)
    return jsonify({"design": str(selection.design)})


def register_avatar_routes(application: Flask) -> None:
    application.add_url_rule("/api/avatars", view_func=list_avatars, methods=["GET"], endpoint="list_avatars")
    application.add_url_rule("/api/avatars", view_func=register_avatar, methods=["POST"], endpoint="register_avatar")
    application.add_url_rule(
        "/api/avatars/<design_id>/image.svg", view_func=avatar_image, methods=["GET"], endpoint="avatar_image"
    )
    application.add_url_rule(
        "/api/avatars/<design_id>/icon.png", view_func=avatar_icon, methods=["GET"], endpoint="avatar_icon"
    )
    application.add_url_rule(
        "/api/avatars/<design_id>/source.svg", view_func=avatar_source, methods=["GET"], endpoint="avatar_source"
    )
    application.add_url_rule(
        "/api/avatar-selection", view_func=select_avatar, methods=["POST"], endpoint="select_avatar"
    )

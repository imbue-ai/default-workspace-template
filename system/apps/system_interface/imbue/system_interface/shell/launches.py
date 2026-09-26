"""Resolving a launch path to the page it opens (post-launch-paths plan sections 3.1 to 3.3).

A GET launch path opens at its own path with the presets and the caller's params as the query string.
A POST launch path is posted to over loopback with the presets, the params, and the shell's envelope
(the requesting client, its desktop, and the path of the window the launch is aimed at), and answers
the path of the page that shows what it did. The shell names no app: what it posts is what the
manifest declares, and all it reads back is a path.
"""

from collections.abc import Callable
from collections.abc import Mapping
from typing import Any
from typing import Final
from typing import assert_never
from urllib.parse import urlencode

import httpx
from app_manifest.manifest import LaunchPathMethod
from app_manifest.registry import RegistryLaunchPath
from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.system_interface.shell.data_types import AppInventoryEntry
from imbue.system_interface.shell.errors import InvalidShellValueError
from imbue.system_interface.shell.errors import LaunchRefusedError
from imbue.system_interface.shell.errors import LaunchUnavailableError
from imbue.system_interface.shell.primitives import ClientId
from imbue.system_interface.shell.primitives import DesktopId
from imbue.system_interface.shell.primitives import WindowPath

# The envelope's field names (the manifest library reserves them for the shell).
CLIENT_ID_FIELD: Final[str] = "client_id"
DESKTOP_ID_FIELD: Final[str] = "desktop_id"
WINDOW_PATH_FIELD: Final[str] = "window_path"
# The one field a POST launch answers.
PATH_FIELD: Final[str] = "path"

# A launch is one loopback request an app answers once it has decided what to show; an app that takes longer
# than this has hung.
LAUNCH_TIMEOUT_SECONDS: Final[float] = 30.0
# How much of an app's answer a refusal quotes.
_DETAIL_LIMIT: Final[int] = 400


class LaunchPost(FrozenModel):
    """One POST launch: where it goes and what it carries."""

    app: str = Field(description="The app the launch path belongs to, for the log")
    url: str = Field(description="The app's registered URL plus the launch path")
    body: dict[str, str] = Field(description="The presets, the params, and the envelope, as one flat object")


class LaunchPostOutcome(FrozenModel):
    """What the app answered a POST launch with: the status and the parsed JSON body (None when it was not JSON)."""

    status_code: int = Field(description="The HTTP status the app answered")
    body: Any = Field(description="The JSON the app answered, or None when the body was not JSON")


LaunchPoster = Callable[[LaunchPost], LaunchPostOutcome]


def post_launch(post: LaunchPost) -> LaunchPostOutcome:
    """Make one POST launch over loopback; an app that cannot be reached or times out raises LaunchUnavailableError."""
    try:
        response = httpx.post(post.url, json=post.body, timeout=LAUNCH_TIMEOUT_SECONDS)
    except httpx.HTTPError as e:
        logger.warning("Could not post a launch to {} at {}: {}", post.app, post.url, e)
        raise LaunchUnavailableError(f"{post.app} could not be reached for the launch: {e}") from e
    try:
        body = response.json()
    except ValueError:
        logger.warning(
            "Launch answer from {} at {} was not JSON ({}): {!r}",
            post.app,
            post.url,
            response.status_code,
            response.text[:_DETAIL_LIMIT],
        )
        body = None
    return LaunchPostOutcome(status_code=response.status_code, body=body)


@pure
def validated_launch_params(launch_path: RegistryLaunchPath, params: Mapping[str, str]) -> dict[str, str]:
    """The caller's values for the params the launch path declares, in manifest order; a name it does not declare
    raises LaunchRefusedError (a 400). The registry row carries the param names alone, so whether a param is
    required is not checked here."""
    declared = [str(param) for param in launch_path.params]
    undeclared = sorted(name for name in params if name not in declared)
    if undeclared:
        raise LaunchRefusedError(
            f"launch path {str(launch_path.id)!r} declares no param {undeclared[0]!r}; it takes {declared}"
        )
    return {name: params[name] for name in declared if name in params}


@pure
def get_launch_destination(launch_path: RegistryLaunchPath, params: Mapping[str, str]) -> WindowPath:
    """Where a GET launch path opens: its path with the presets and the params as the query string."""
    query = {**{str(name): value for name, value in launch_path.presets.items()}, **params}
    encoded = urlencode(query)
    try:
        return WindowPath(f"{launch_path.path}?{encoded}" if encoded else str(launch_path.path))
    except InvalidShellValueError as e:
        raise LaunchRefusedError(f"the launch path with its params is not a window path: {e}") from e


@pure
def launch_post_body(
    launch_path: RegistryLaunchPath,
    params: Mapping[str, str],
    client_id: ClientId | None,
    desktop_id: DesktopId | None,
    window_path: WindowPath | None,
) -> dict[str, str]:
    """What a POST launch carries: the presets, then the params, then the envelope's fields that are known."""
    body: dict[str, str] = {**{str(name): value for name, value in launch_path.presets.items()}, **params}
    if client_id is not None:
        body[CLIENT_ID_FIELD] = str(client_id)
    if desktop_id is not None:
        body[DESKTOP_ID_FIELD] = str(desktop_id)
    if window_path is not None:
        body[WINDOW_PATH_FIELD] = str(window_path)
    return body


@pure
def launch_post(entry: AppInventoryEntry, launch_path: RegistryLaunchPath, body: Mapping[str, str]) -> LaunchPost:
    return LaunchPost(
        app=str(entry.row.name), url=f"{str(entry.row.url).rstrip('/')}{launch_path.path}", body=dict(body)
    )


@pure
def _quoted_detail(body: Any) -> str:
    if isinstance(body, dict) and isinstance(body.get("detail"), str):
        return body["detail"][:_DETAIL_LIMIT]
    return ""


@pure
def launched_path(app: str, outcome: LaunchPostOutcome) -> WindowPath:
    """The page path a POST launch answered. A 4xx carrying a ``detail`` is the app's refusal, passed on as
    LaunchRefusedError with that detail; anything else that is not a 200 carrying a window path (a 4xx with no
    detail among them: a missing or GET-only route, not a refusal the app wrote) is LaunchUnavailableError."""
    if 400 <= outcome.status_code < 500:
        detail = _quoted_detail(outcome.body)
        if detail:
            raise LaunchRefusedError(f"{app} refused the launch: {detail}")
        raise LaunchUnavailableError(f"{app} answered the launch with {outcome.status_code} and no refusal detail")
    if outcome.status_code != 200:
        raise LaunchUnavailableError(f"{app} answered the launch with {outcome.status_code} instead of a page path")
    answered = outcome.body.get(PATH_FIELD) if isinstance(outcome.body, dict) else None
    if not isinstance(answered, str):
        raise LaunchUnavailableError(f"{app} answered the launch with no page path")
    try:
        return WindowPath(answered)
    except InvalidShellValueError as e:
        raise LaunchUnavailableError(f"{app} answered the launch with a path that is not a window path: {e}") from e


def resolve_launch_destination(
    entry: AppInventoryEntry,
    launch_path: RegistryLaunchPath,
    params: Mapping[str, str],
    client_id: ClientId | None,
    desktop_id: DesktopId | None,
    window_path: WindowPath | None,
    poster: LaunchPoster,
) -> WindowPath:
    """The page a launch of ``launch_path`` opens: built for a GET, asked of the app for a POST."""
    checked = validated_launch_params(launch_path, params)
    match launch_path.method:
        case LaunchPathMethod.GET:
            return get_launch_destination(launch_path, checked)
        case LaunchPathMethod.POST:
            body = launch_post_body(launch_path, checked, client_id, desktop_id, window_path)
            outcome = poster(launch_post(entry, launch_path, body))
            return launched_path(str(entry.row.name), outcome)
        case _ as unreachable:
            assert_never(unreachable)

"""What every route of the shell exchanges: the table its API document publishes and every parse and answer is
checked against (``app_schema``)."""

from typing import Final

from app_manifest.registry import APP_CONTRACT_ROUTE
from app_manifest.registry import CONTEXT_MENU_ROUTE
from app_schema.api_schema import AlternateResponse
from app_schema.api_schema import RouteModels
from app_schema.api_schema import RouteTable
from app_schema.api_schema import SchemalessRoute
from pydantic import BaseModel
from workspace_layout.answers import ClientView
from workspace_layout.answers import ClientsListing
from workspace_layout.answers import ContextAnswer
from workspace_layout.answers import DesktopOpAnswer
from workspace_layout.answers import DesktopsListing
from workspace_layout.answers import InventoryDocument
from workspace_layout.answers import InventoryOpAnswer
from workspace_layout.answers import OpenAnswer
from workspace_layout.answers import ShowAnswer
from workspace_layout.answers import TransientOpAnswer
from workspace_layout.ops import ClientActivityReport
from workspace_layout.records import DesktopLayoutView
from workspace_layout.records import DesktopView
from workspace_layout.records import EntryPresentation
from workspace_layout.records import WindowView
from workspace_layout.shell_url import CLIENTS_ROUTE
from workspace_layout.shell_url import CLIENT_ACTIVITY_ROUTE
from workspace_layout.shell_url import DESKTOPS_ROUTE
from workspace_layout.shell_url import INVENTORY_ROUTE
from workspace_layout.shell_url import LAYOUT_OP_ROUTE
from workspace_themes.flask_routes import THEME_STATIC_ROUTE_TABLE

from imbue.system_interface.api_models import HeartbeatResponse
from imbue.system_interface.api_models import PresenceListing
from imbue.system_interface.api_models import ShellHealthResponse
from imbue.system_interface.avatar.api_models import AvatarRegistered
from imbue.system_interface.avatar.api_models import AvatarSelected
from imbue.system_interface.avatar.api_models import AvatarsListing
from imbue.system_interface.avatar.catalog import DesignRegistration
from imbue.system_interface.avatar.selection import AvatarSelection
from imbue.system_interface.documents import MANIFEST_PATH
from imbue.system_interface.documents import THEME_GALLERY_ROUTE
from imbue.system_interface.documents import TOUCH_ICON_PATH
from imbue.system_interface.shell.api_models import AppRunningState
from imbue.system_interface.shell.api_models import ClientArrivalResponse
from imbue.system_interface.shell.api_models import DesktopDeletedResponse
from imbue.system_interface.shell.api_models import EmbedderMessageRelayResponse
from imbue.system_interface.shell.api_models import LaunchResponse
from imbue.system_interface.shell.api_models import LayoutOpRequest
from imbue.system_interface.shell.api_models import PendingUpdateResponse
from imbue.system_interface.shell.api_models import PlacementsSavedResponse
from imbue.system_interface.shell.api_models import UpdateRollbackStarted
from imbue.system_interface.shell.api_models import WindowOpenedResponse
from imbue.system_interface.shell.data_types import ClientShownRequest
from imbue.system_interface.shell.data_types import LaunchRequest
from imbue.system_interface.shell.data_types import PlacementsSaveRequest
from imbue.system_interface.shell.data_types import WindowLocationReport
from imbue.system_interface.shell.data_types import WindowOpenRequest
from imbue.system_interface.shell.desktop_routes import DesktopMetadataRequest
from imbue.system_interface.shell.desktop_routes import DesktopSettingsRequest
from imbue.system_interface.shell.desktop_routes import DesktopShortcutRequest
from imbue.system_interface.shell.desktop_routes import DesktopWallpaperRequest
from imbue.system_interface.shell.desktop_routes import ShortcutMoveRequest
from imbue.system_interface.shell.desktop_routes import ShortcutRemoveRequest
from imbue.system_interface.shell.embedder_messages import EmbedderMessageRelayRequest
from imbue.system_interface.shell.route_helpers import HTTP_ACCEPTED
from imbue.system_interface.shell.route_helpers import HTTP_BAD_GATEWAY
from imbue.system_interface.shell.route_helpers import HTTP_CREATED
from imbue.system_interface.shell.route_helpers import HTTP_NO_CONTENT
from imbue.system_interface.shell.theme_routes import DefaultThemeResponse
from imbue.system_interface.shell.theme_routes import ThemeChoiceRequest
from imbue.system_interface.shell.theme_wire import THEME_ICON_ROUTE
from imbue.system_interface.shell.theme_wire import ThemeCatalogWire
from imbue.system_interface.shell.tour import TOUR_ROUTE
from imbue.system_interface.shell.tour import TourProgress
from imbue.system_interface.shell.wallpapers import WALLPAPER_ROUTE_PREFIX
from imbue.system_interface.shell.wallpapers import WallpapersListing

PRESENCE_PATH: Final[str] = "/api/presence"
PRESENCE_HEARTBEAT_PATH: Final[str] = "/api/presence/heartbeat"

_PAGE: Final[SchemalessRoute] = SchemalessRoute(reason="the workspace's page")
_SHELL_MODULE: Final[SchemalessRoute] = SchemalessRoute(reason="a browser-side module the shell builds")
_IMAGE: Final[SchemalessRoute] = SchemalessRoute(reason="an image")


def _edits_desktop(request_model: type[BaseModel]) -> RouteModels:
    """A desktop edit: the body it takes, and the desktop after the edit."""
    return RouteModels(request_model=request_model, response_model=DesktopView)


SHELL_ROUTE_TABLE: RouteTable = {
    ("GET", "/"): _PAGE,
    ("GET", "/{path}"): SchemalessRoute(
        reason="the workspace's page for a path the page routes on its own; an unknown API path is a JSON 404"
    ),
    ("GET", "/favicon.ico"): _IMAGE,
    ("GET", TOUCH_ICON_PATH): _IMAGE,
    ("GET", MANIFEST_PATH): SchemalessRoute(reason="the web app manifest"),
    ("GET", "/assets/{filename}"): SchemalessRoute(reason="a file of the page's built bundle"),
    ("GET", APP_CONTRACT_ROUTE): _SHELL_MODULE,
    ("GET", CONTEXT_MENU_ROUTE): _SHELL_MODULE,
    ("GET", "/api/ws"): SchemalessRoute(reason="the shell's socket, which every page holds"),
    ("GET", "/api/health"): RouteModels(response_model=ShellHealthResponse),
    ("GET", PRESENCE_PATH): RouteModels(response_model=PresenceListing),
    ("POST", PRESENCE_HEARTBEAT_PATH): RouteModels(
        response_model=HeartbeatResponse,
        alternate_responses=(AlternateResponse(status=HTTP_NO_CONTENT),),
    ),
    # Section 5.1 and 5: the client-activity report, and an app's lifecycle
    ("POST", CLIENT_ACTIVITY_ROUTE): RouteModels(request_model=ClientActivityReport, success_status=HTTP_NO_CONTENT),
    ("POST", "/api/apps/{name}/stop"): RouteModels(response_model=AppRunningState),
    ("POST", "/api/apps/{name}/start"): RouteModels(response_model=AppRunningState),
    ("POST", "/api/apps/{name}/quit"): RouteModels(response_model=AppRunningState),
    # Section 5.2: desktops
    ("GET", DESKTOPS_ROUTE): RouteModels(response_model=DesktopsListing),
    ("POST", DESKTOPS_ROUTE): RouteModels(
        request_model=DesktopMetadataRequest, response_model=DesktopView, success_status=HTTP_CREATED
    ),
    ("POST", "/api/desktops/{desktop_id}/settings"): _edits_desktop(DesktopSettingsRequest),
    ("POST", "/api/desktops/{desktop_id}/wallpaper"): _edits_desktop(DesktopWallpaperRequest),
    ("POST", "/api/desktops/{desktop_id}/delete"): RouteModels(response_model=DesktopDeletedResponse),
    ("POST", "/api/desktops/{desktop_id}/shortcuts"): _edits_desktop(DesktopShortcutRequest),
    ("POST", "/api/desktops/{desktop_id}/shortcuts/move"): _edits_desktop(ShortcutMoveRequest),
    ("POST", "/api/desktops/{desktop_id}/shortcuts/remove"): _edits_desktop(ShortcutRemoveRequest),
    # Section 5.3: windows
    ("POST", "/api/desktops/{desktop_id}/windows"): RouteModels(
        request_model=WindowOpenRequest,
        response_model=WindowOpenedResponse,
        alternate_responses=(AlternateResponse(status=HTTP_CREATED, model=WindowOpenedResponse),),
    ),
    ("POST", "/api/desktops/{desktop_id}/launch"): RouteModels(
        request_model=LaunchRequest,
        response_model=LaunchResponse,
        alternate_responses=(AlternateResponse(status=HTTP_CREATED, model=LaunchResponse),),
    ),
    ("POST", "/api/desktops/{desktop_id}/windows/{window_id}/close"): RouteModels(success_status=HTTP_NO_CONTENT),
    ("POST", "/api/desktops/{desktop_id}/windows/{window_id}/location"): RouteModels(
        request_model=WindowLocationReport, response_model=WindowView
    ),
    # Section 5.4: placements
    ("GET", "/api/placements/{desktop_id}"): RouteModels(response_model=DesktopLayoutView),
    ("POST", "/api/placements/{desktop_id}"): RouteModels(
        request_model=PlacementsSaveRequest, response_model=PlacementsSavedResponse
    ),
    # Section 5.5: the arrival, the clients, wallpapers, and the inventory document
    ("POST", "/api/clients/{client_id}/arrive"): RouteModels(response_model=ClientArrivalResponse),
    ("GET", CLIENTS_ROUTE): RouteModels(response_model=ClientsListing),
    ("POST", "/api/clients/{client_id}/entries/{app}"): RouteModels(
        request_model=EntryPresentation, response_model=ClientView
    ),
    ("POST", "/api/clients/{client_id}/shown"): RouteModels(
        request_model=ClientShownRequest, response_model=ClientView
    ),
    ("GET", INVENTORY_ROUTE): RouteModels(response_model=InventoryDocument),
    ("GET", "/api/wallpapers"): RouteModels(response_model=WallpapersListing),
    ("GET", f"{WALLPAPER_ROUTE_PREFIX}/{{kind}}/{{name}}"): _IMAGE,
    # The workspace-themes plan: the gallery, the catalog and its default, each desktop's theme, icons, and theme files
    ("GET", THEME_GALLERY_ROUTE): SchemalessRoute(reason="the theme gallery page"),
    ("GET", "/api/themes"): RouteModels(response_model=ThemeCatalogWire),
    ("POST", "/api/themes/default"): RouteModels(
        request_model=ThemeChoiceRequest, response_model=DefaultThemeResponse
    ),
    ("POST", "/api/desktops/{desktop_id}/theme"): _edits_desktop(ThemeChoiceRequest),
    ("GET", THEME_ICON_ROUTE): _IMAGE,
    **THEME_STATIC_ROUTE_TABLE,
    # Section 5.6: the embedder-message relay
    ("POST", "/api/embedder-messages"): RouteModels(
        request_model=EmbedderMessageRelayRequest,
        response_model=EmbedderMessageRelayResponse,
        alternate_responses=(AlternateResponse(status=HTTP_BAD_GATEWAY, model=EmbedderMessageRelayResponse),),
    ),
    # The update notice
    ("GET", "/api/updates/pending"): RouteModels(response_model=PendingUpdateResponse),
    ("POST", "/api/updates/pending/confirm"): RouteModels(success_status=HTTP_NO_CONTENT),
    ("POST", "/api/updates/pending/rollback"): RouteModels(
        response_model=UpdateRollbackStarted, success_status=HTTP_ACCEPTED
    ),
    # Section 8: the agent-facing op route, whose body the op parser reads (its refusals are the CLI's contract)
    ("POST", LAYOUT_OP_ROUTE): RouteModels(
        request_model=LayoutOpRequest,
        response_model=DesktopOpAnswer,
        alternate_responses=(
            AlternateResponse(status=200, model=OpenAnswer),
            AlternateResponse(status=200, model=ShowAnswer),
            AlternateResponse(status=200, model=TransientOpAnswer),
            AlternateResponse(status=200, model=ContextAnswer),
            AlternateResponse(status=200, model=InventoryOpAnswer),
        ),
        is_body_parsed_by_view=True,
    ),
    # The avatar
    ("GET", "/api/avatars"): RouteModels(response_model=AvatarsListing),
    ("POST", "/api/avatars"): RouteModels(
        request_model=DesignRegistration, response_model=AvatarRegistered, success_status=HTTP_CREATED
    ),
    ("GET", "/api/avatars/{design_id}/image.svg"): _IMAGE,
    ("GET", "/api/avatars/{design_id}/icon.png"): _IMAGE,
    ("GET", "/api/avatars/{design_id}/source.svg"): SchemalessRoute(reason="the design's SVG, as a download"),
    ("POST", "/api/avatar-selection"): RouteModels(request_model=AvatarSelection, response_model=AvatarSelected),
    # The show-around tour
    ("GET", TOUR_ROUTE): RouteModels(response_model=TourProgress),
    ("POST", TOUR_ROUTE): RouteModels(request_model=TourProgress, response_model=TourProgress),
}

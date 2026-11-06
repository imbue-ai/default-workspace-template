"""The shell's Web Push routes (README, "Notifications on a phone"): the page that turns notifications on for the
device it is opened on, the service worker that shows them, the device registrations behind both, and the loopback
route the workspace's agents and apps send through.

The page and the worker are served from the shell's root because a push subscription belongs to the origin and the
service worker scope it was made under, and the shell's root is what a phone saves to its home screen. Only the
owner registers a device: what the workspace's agents send is the owner's, so a visitor's device never receives it."""

from pathlib import Path
from typing import Final

from app_schema.routes import model_response
from app_schema.routes import parse_request_body
from flask import Flask
from flask import Response
from flask import send_file
from flask.typing import ResponseReturnValue
from workspace_layout.identity import is_owner_participant

from imbue.system_interface.app_context import get_state
from imbue.system_interface.presence import utc_now
from imbue.system_interface.shell.route_helpers import HTTP_CREATED
from imbue.system_interface.shell.route_helpers import HTTP_FORBIDDEN
from imbue.system_interface.shell.route_helpers import HTTP_NOT_FOUND
from imbue.system_interface.shell.route_helpers import HTTP_NO_CONTENT
from imbue.system_interface.shell.route_helpers import detail_response
from imbue.system_interface.shell.route_helpers import refuse_if_preview
from imbue.system_interface.shell.route_helpers import request_identity
from imbue.system_interface.shell.route_helpers import require_loopback
from imbue.system_interface.shell.web_push import DeviceRegistrationRequest
from imbue.system_interface.shell.web_push import DevicesListing
from imbue.system_interface.shell.web_push import PushNotificationRequest
from imbue.system_interface.shell.web_push import RegisteredDevice
from imbue.system_interface.shell.web_push import WebPushNotifier
from imbue.system_interface.shell.web_push import WebPushPublicKey
from imbue.system_interface.shell.web_push import device_view

NOTIFICATIONS_PAGE_ROUTE: Final[str] = "/notifications"
NOTIFICATIONS_SCRIPT_ROUTE: Final[str] = "/notifications.js"
WORKER_SCRIPT_ROUTE: Final[str] = "/sw.js"
WEB_PUSH_KEY_ROUTE: Final[str] = "/api/web-push/key"
WEB_PUSH_DEVICES_ROUTE: Final[str] = "/api/web-push/devices"
WEB_PUSH_SEND_ROUTE: Final[str] = "/api/web-push/send"

_ASSETS_DIRECTORY: Final[Path] = Path(__file__).parent / "web_push_assets"

OWNER_ONLY_DETAIL: Final[str] = "Only the workspace's owner can turn notifications on here."
PROXIED_SEND_DETAIL: Final[str] = "Notifications are sent from inside the workspace, never through a share."
WEB_PUSH_PREVIEW_DETAIL: Final[str] = "This is a preview of a proposed change; it sends no notifications."

TEST_NOTIFICATION: Final[PushNotificationRequest] = PushNotificationRequest(
    title="Notifications are on",
    body="This device will get a notification when your workspace has something for you.",
    path="/",
    tag="web-push-test",
)


def _web_push() -> WebPushNotifier:
    return get_state().shell.web_push


def _asset_response(filename: str, mimetype: str) -> Response:
    response = send_file(_ASSETS_DIRECTORY / filename, mimetype=mimetype)
    # A cached worker or page would keep running old code after the shell is updated.
    response.headers["Cache-Control"] = "no-cache"
    return response


def notifications_page() -> Response:
    """The page that turns notifications on (and off) for the device it is opened on, and sends it a test."""
    return _asset_response("notifications.html", "text/html")


def notifications_script() -> Response:
    """The notifications page's script."""
    return _asset_response("notifications.js", "text/javascript")


def worker_script() -> Response:
    """The service worker that shows a pushed notification and opens the workspace when it is tapped."""
    return _asset_response("sw.js", "text/javascript")


def _refuse_unless_owner() -> ResponseReturnValue | None:
    if not is_owner_participant(request_identity()):
        return detail_response(OWNER_ONLY_DETAIL, HTTP_FORBIDDEN)
    return None


def _refuse_device_change() -> ResponseReturnValue | None:
    """A device registration, removal, or test is the owner's, and a preview's would reach the live devices."""
    refusal = _refuse_unless_owner()
    if refusal is not None:
        return refusal
    return refuse_if_preview(WEB_PUSH_PREVIEW_DETAIL)


def _registered_device(web_push: WebPushNotifier, device_id: str) -> RegisteredDevice | None:
    """The registered device a route's URL names; None for an id no device has, a malformed one included."""
    return next((device for device in web_push.list_devices() if device.device_id == device_id), None)


def get_public_key() -> ResponseReturnValue:
    """The key a browser subscribes with."""
    return model_response(WebPushPublicKey(public_key=_web_push().public_key()))


def list_devices() -> ResponseReturnValue:
    """Every device that turned notifications on; the owner's alone to see."""
    refusal = _refuse_unless_owner()
    if refusal is not None:
        return refusal
    return model_response(DevicesListing(devices=tuple(device_view(device) for device in _web_push().list_devices())))


def register_device() -> ResponseReturnValue:
    """Keep the subscription the requesting device just made; the owner's alone."""
    refusal = _refuse_device_change()
    if refusal is not None:
        return refusal
    device = _web_push().register(parse_request_body(DeviceRegistrationRequest), utc_now())
    return model_response(device_view(device), HTTP_CREATED)


def forget_device(device_id: str) -> ResponseReturnValue:
    """Forget a device, so it gets no more notifications; the owner's alone."""
    refusal = _refuse_device_change()
    if refusal is not None:
        return refusal
    web_push = _web_push()
    device = _registered_device(web_push, device_id)
    if device is None or not web_push.forget(frozenset({device.device_id})):
        return detail_response(f"No registered device {device_id!r}", HTTP_NOT_FOUND)
    return "", HTTP_NO_CONTENT


def send_test_notification(device_id: str) -> ResponseReturnValue:
    """Send one device a test notification and answer what its push service said; the owner's alone."""
    refusal = _refuse_device_change()
    if refusal is not None:
        return refusal
    web_push = _web_push()
    device = _registered_device(web_push, device_id)
    if device is None:
        return detail_response(f"No registered device {device_id!r}", HTTP_NOT_FOUND)
    return model_response(web_push.send(TEST_NOTIFICATION, frozenset({device.device_id})))


def send_notification() -> ResponseReturnValue:
    """Show a notification on every device the owner registered: loopback only, for the workspace's own agents and
    apps, and never through a share's proxy."""
    refusal = require_loopback()
    if refusal is None:
        refusal = refuse_if_preview(WEB_PUSH_PREVIEW_DETAIL)
    if refusal is not None:
        return refusal
    if request_identity().is_header_present:
        return detail_response(PROXIED_SEND_DETAIL, HTTP_FORBIDDEN)
    return model_response(_web_push().send(parse_request_body(PushNotificationRequest), None))


def register_web_push_routes(application: Flask) -> None:
    """Wire the Web Push routes onto the Flask application."""
    application.add_url_rule(NOTIFICATIONS_PAGE_ROUTE, view_func=notifications_page, methods=["GET"])
    application.add_url_rule(NOTIFICATIONS_SCRIPT_ROUTE, view_func=notifications_script, methods=["GET"])
    application.add_url_rule(WORKER_SCRIPT_ROUTE, view_func=worker_script, methods=["GET"])
    application.add_url_rule(WEB_PUSH_KEY_ROUTE, view_func=get_public_key, methods=["GET"])
    application.add_url_rule(WEB_PUSH_DEVICES_ROUTE, view_func=list_devices, methods=["GET"])
    application.add_url_rule(WEB_PUSH_DEVICES_ROUTE, view_func=register_device, methods=["POST"])
    application.add_url_rule(f"{WEB_PUSH_DEVICES_ROUTE}/<device_id>", view_func=forget_device, methods=["DELETE"])
    application.add_url_rule(
        f"{WEB_PUSH_DEVICES_ROUTE}/<device_id>/test", view_func=send_test_notification, methods=["POST"]
    )
    application.add_url_rule(WEB_PUSH_SEND_ROUTE, view_func=send_notification, methods=["POST"])

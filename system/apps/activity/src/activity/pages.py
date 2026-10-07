"""The app's routes on its own origin: the page, the health probe, the memory summary, storage, the chat actions,
and the contract module.

Everything is read when asked: the app keeps nothing between requests, so the shell can stop it once no window
shows it and nothing is lost. Only the workspace's owner reaches the API, and a write must come from this app's own
page (``request_guard``).
"""

import threading
from collections.abc import Callable
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Final

import httpx
from flask import Blueprint
from flask import Response
from flask import jsonify
from flask import request
from flask import send_file
from flask import send_from_directory
from flask.typing import ResponseReturnValue
from werkzeug.exceptions import NotFound

from activity.chats import ChatAction
from activity.chats import chat_app_url
from activity.chats import fetch_chats
from activity.chats import request_chat_action
from activity.commands import RunCommand
from activity.errors import ChatAppUnavailableError
from activity.history import HistoryRange
from activity.history_view import collect_history_view
from activity.readings import ReadingSources
from activity.readings import collect_summary_inputs
from activity.readings import read_app_rows
from activity.request_guard import IDENTITY_HEADER
from activity.request_guard import SAFE_METHODS
from activity.request_guard import is_owner_request
from activity.request_guard import is_write_allowed
from activity.storage import measure_storage
from activity.summary import build_summary
from activity.supervised_programs import ReadProcessInfo
from app_manifest.primitives import AppName
from app_manifest.registry import APP_CONTRACT_ROUTE
from app_manifest.registry import read_origin_label

APP_NAME: Final[AppName] = AppName("activity")
BLUEPRINT_NAME: Final[str] = "activity_pages"
API_PREFIX: Final[str] = "/api/"
HEALTH_PATH: Final[str] = "/api/health"
SUMMARY_PATH: Final[str] = "/api/summary"
STORAGE_PATH: Final[str] = "/api/storage"
HISTORY_PATH_ROUTE: Final[str] = "/api/history"
HTTP_BAD_REQUEST: Final[int] = 400
PAGE_DOCUMENT_FILENAME: Final[str] = "index.html"
# A preview (``--no-register``) shares the live workspace's chats and desktop, so it changes neither; the shell's
# preview refuses in the same words.
PREVIEW_REFUSAL: Final[str] = "This is a preview of a proposed change; it cannot change the live workspace."
# The chat app's status for a chat in the middle of a turn: stopping it interrupts the turn.
WORKING_CHAT_STATUS: Final[str] = "working"

HTTP_FORBIDDEN: Final[int] = 403
HTTP_NOT_FOUND: Final[int] = 404
HTTP_CONFLICT: Final[int] = 409
HTTP_TOO_MANY_REQUESTS: Final[int] = 429
HTTP_BAD_GATEWAY: Final[int] = 502

_NOT_BUILT_PAGE: Final[str] = (
    '<!doctype html><html><head><meta charset="utf-8"><title>System Monitor</title></head>'
    "<body><p>The System Monitor page has not been built yet (run <code>npm run build</code> in <code>system/</code>).</p>"
    "</body></html>"
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def build_pages_blueprint(
    static_directory: Path,
    contract_path: Path,
    sources: ReadingSources,
    client: httpx.Client,
    read_process_info: ReadProcessInfo,
    run_command: RunCommand,
    now: Callable[[], datetime],
    data_dir: Path,
    clock: Callable[[], float],
    history_path: Path,
    shed_ledger_path: Path,
    is_preview: bool,
) -> Blueprint:
    blueprint = Blueprint(BLUEPRINT_NAME, __name__)
    # One du at a time: it walks every file, and a second request while one runs would only double the work.
    storage_lock = threading.Lock()

    @blueprint.before_request
    def guard() -> ResponseReturnValue | None:
        if request.path.startswith(API_PREFIX) and request.path != HEALTH_PATH:
            if not is_owner_request(request.headers.get(IDENTITY_HEADER)):
                return jsonify({"detail": "System Monitor is only available to the workspace's owner"}), HTTP_FORBIDDEN
        fetch_site = request.headers.get("Sec-Fetch-Site")
        origin = request.headers.get("Origin")
        # Only a browser too old to send Sec-Fetch-Site needs the app's origin label, so read it only then.
        needs_label = request.method not in SAFE_METHODS and origin is not None and fetch_site is None
        app_origin_label = (read_origin_label(sources.registry_path, APP_NAME) or None) if needs_label else None
        if not is_write_allowed(request.method, origin, fetch_site, request.content_type, app_origin_label):
            return jsonify({"detail": "writes must be JSON and come from this app's own page"}), HTTP_FORBIDDEN
        return None

    @blueprint.get("/")
    def page() -> ResponseReturnValue:
        document = static_directory / PAGE_DOCUMENT_FILENAME
        if not document.is_file():
            return Response(_NOT_BUILT_PAGE, mimetype="text/html", headers={"Cache-Control": "no-store"})
        response = send_file(document.absolute(), mimetype="text/html")
        response.headers["Cache-Control"] = "no-store"
        return response

    @blueprint.get("/assets/<path:filename>")
    def asset(filename: str) -> ResponseReturnValue:
        try:
            return send_from_directory((static_directory / "assets").absolute(), filename)
        except NotFound:
            return Response(status=HTTP_NOT_FOUND)

    @blueprint.get(HEALTH_PATH)
    def health() -> ResponseReturnValue:
        return jsonify({"status": "ok", "is_frontend_built": (static_directory / PAGE_DOCUMENT_FILENAME).is_file()})

    @blueprint.get(SUMMARY_PATH)
    def summary() -> ResponseReturnValue:
        inputs = collect_summary_inputs(
            sources=sources, client=client, read_process_info=read_process_info, now=now(), is_preview=is_preview
        )
        response = jsonify(build_summary(inputs).model_dump(mode="json"))
        response.headers["Cache-Control"] = "no-store"
        return response

    @blueprint.get(HISTORY_PATH_ROUTE)
    def history() -> ResponseReturnValue:
        try:
            history_range = HistoryRange(request.args.get("range", HistoryRange.DAY.value).upper())
        except ValueError:
            return jsonify({"detail": "range must be hour, day or week"}), HTTP_BAD_REQUEST
        view = collect_history_view(
            history_range=history_range,
            memory_sources=sources.memory,
            proc_dir=sources.proc_dir,
            history_path=history_path,
            ledger_path=shed_ledger_path,
            now=now(),
        )
        response = jsonify(view.model_dump(mode="json"))
        response.headers["Cache-Control"] = "no-store"
        return response

    @blueprint.get(STORAGE_PATH)
    def storage() -> ResponseReturnValue:
        if not storage_lock.acquire(blocking=False):
            return jsonify({"detail": "already measuring; try again in a moment"}), HTTP_TOO_MANY_REQUESTS
        try:
            summary = measure_storage(data_dir=data_dir, run_command=run_command, now=now, clock=clock)
        finally:
            storage_lock.release()
        response = jsonify(summary.model_dump(mode="json"))
        response.headers["Cache-Control"] = "no-store"
        return response

    @blueprint.post("/api/chats/<chat_id>/<action>")
    def chat_action(chat_id: str, action: str) -> ResponseReturnValue:
        if is_preview:
            return jsonify({"detail": PREVIEW_REFUSAL}), HTTP_FORBIDDEN
        try:
            chat_action_kind = ChatAction(action.upper())
        except ValueError:
            return jsonify({"detail": f"unknown chat action {action!r}"}), HTTP_NOT_FOUND
        body = request.get_json(silent=True)
        is_interrupt_confirmed = isinstance(body, dict) and body.get("is_interrupt_confirmed") is True
        # As the summary does, an unreadable registry falls back to the chat app's default port.
        rows, _ = read_app_rows(sources.registry_path)
        base_url = chat_app_url(rows)
        try:
            # Only a chat the chat app itself lists is acted on, and its state is read now: the page's view may be
            # seconds old, and a chat that was idle then may be mid-turn now.
            chat = next((item for item in fetch_chats(client, base_url) if item.chat_id == chat_id), None)
            if chat is None:
                return jsonify({"detail": f"no chat {chat_id}"}), HTTP_NOT_FOUND
            if (
                chat_action_kind is ChatAction.STOP
                and chat.status == WORKING_CHAT_STATUS
                and not is_interrupt_confirmed
            ):
                return (
                    jsonify(
                        {"detail": f"{chat.title} started working since the page read it", "chat_status": chat.status}
                    ),
                    HTTP_CONFLICT,
                )
            request_chat_action(client, base_url, chat.chat_id, chat_action_kind)
        except ChatAppUnavailableError as e:
            return jsonify({"detail": str(e)}), HTTP_BAD_GATEWAY
        return jsonify({"status": "ok"})

    @blueprint.get(APP_CONTRACT_ROUTE)
    def app_contract() -> ResponseReturnValue:
        if not contract_path.is_file():
            return (
                jsonify({"detail": f"the workspace shell's frontend is not built: {contract_path} is missing"}),
                HTTP_NOT_FOUND,
            )
        return send_file(contract_path.absolute(), mimetype="text/javascript")

    return blueprint

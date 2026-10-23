"""The shell's client-activity report for a send: which client (and desktop) a message to a chat came from, so agents
can attribute requests through ``workspace-layout context`` (desktop contracts.md section 5.1).

Every path a person's message enters a chat through reports it the same way, whatever the chat's kind: the send
path (which a room's message takes as an agent chat's does) and the switch route. The log is the shell's and is keyed
by chat id, so the report names the chat alone; a caller naming no client or no desktop (a script, a legacy page) is
not recorded, and neither is anything a secondary chat (a preview) hears.
"""

import threading

from loguru import logger
from workspace_layout.errors import InvalidLayoutValueError
from workspace_layout.interfaces import ShellLayoutInterface
from workspace_layout.ops import ClientActivityReport
from workspace_layout.primitives import ClientActivityKind
from workspace_layout.primitives import ClientId
from workspace_layout.primitives import DesktopId

from imbue.chat.models import SendMessageRequest
from imbue.chat.primitives import CHAT_APP_NAME
from imbue.chat.primitives import ChatId
from imbue.imbue_common.pure import pure


@pure
def is_client_activity_reportable(send_message_request: SendMessageRequest) -> bool:
    """Whether a send names the client and the desktop the shell's activity log keys on (a legacy or unframed caller names neither)."""
    return send_message_request.client_id != "" and send_message_request.desktop_id != ""


@pure
def client_activity_report(chat_id: ChatId, send_message_request: SendMessageRequest) -> ClientActivityReport:
    """The shell's client-activity report for one send (desktop contracts.md section 5.1), keyed by chat. Raises
    InvalidLayoutValueError when the send's client or desktop is not an id the shell would take."""
    return ClientActivityReport(
        client_id=ClientId(send_message_request.client_id),
        desktop_id=DesktopId(send_message_request.desktop_id),
        kind=ClientActivityKind.MESSAGE,
        app=str(CHAT_APP_NAME),
        key=chat_id,
        text=send_message_request.message,
    )


def record_client_message_activity(
    shell: ShellLayoutInterface, chat_id: ChatId, send_message_request: SendMessageRequest, is_secondary: bool
) -> None:
    """Tell the shell which client (and desktop) a message came from, so agents can attribute requests through
    ``workspace-layout context``. Callers naming no client or no desktop are not recorded, and neither is anything a
    secondary chat (a preview) hears: the shell's activity log is the live chat's. Reported on its own thread:
    the shell is a separate app, and a send must not wait on it."""
    if is_secondary or not is_client_activity_reportable(send_message_request):
        return
    try:
        report = client_activity_report(chat_id, send_message_request)
    except InvalidLayoutValueError as e:
        logger.warning("Skipped reporting a send of chat {} to the shell: {}", chat_id, e)
        return
    threading.Thread(
        target=shell.record_client_activity, args=(report,), name="client-activity-report", daemon=True
    ).start()

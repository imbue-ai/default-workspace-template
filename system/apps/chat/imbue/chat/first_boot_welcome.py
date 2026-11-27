"""A new workspace's welcome chat, opened by the workspace itself.

The bootstrap marks a workspace that is booting for the first time as owing its owner a welcome chat
(``OWES_WELCOME_CHAT_RELATIVE_PATH``). This app opens that chat once it has read its agent list: the same first-run
chat the seed route opens for a client (``POST /api/chats/seed`` with ``is_first_run``), so a workspace gets one
whichever client created it, and one created with no client watching gets one too.

A client from before this may still post its own first-run seed. Whichever comes second finds the chat there: the
seed route answers the chat that exists, and this module reads a taken name as the chat having been opened.
"""

import threading
from pathlib import Path
from typing import Final

from loguru import logger

from imbue.chat.agent_manager import AgentManager
from imbue.chat.chat_seed import FIRST_RUN_WELCOME
from imbue.chat.main_chat import MAIN_CHAT_NAME
from imbue.chat.models import AgentCreationError
from imbue.chat.models import AgentNameConflictError

# Written by the bootstrap (``OWE_WELCOME_CHAT_SIGNAL``) on a workspace's first boot, relative to the workspace's
# root, and removed here once the chat is open.
OWES_WELCOME_CHAT_RELATIVE_PATH: Final[Path] = Path("data/.state/chat/owes_welcome_chat")

_AGENT_LIST_WAIT_SECONDS: Final[float] = 0.25


def open_welcome_chat_owed_from_first_boot(
    agent_manager: AgentManager,
    owes_welcome_chat_path: Path,
    # Set when the app is shutting down, so the wait for the agent list ends with it.
    is_stopping: threading.Event,
) -> None:
    """Open the first-run chat a first boot left owed, once the agent list is known, and settle the debt.

    The debt stays when the chat could not be opened, so the next start of this app tries again.
    """
    if not owes_welcome_chat_path.exists():
        return
    while not agent_manager.is_agent_list_known():
        if is_stopping.wait(_AGENT_LIST_WAIT_SECONDS):
            return
    try:
        created = agent_manager.seed_chat(MAIN_CHAT_NAME, FIRST_RUN_WELCOME, first_run_context="", is_first_run=True)
    except AgentNameConflictError:
        logger.info("The welcome chat is already open")
    except (AgentCreationError, OSError) as e:
        logger.warning("Could not open the welcome chat a first boot owes: {}", e)
        return
    else:
        logger.info("Opened the welcome chat {} for a workspace booting for the first time", created.chat_id)
    owes_welcome_chat_path.unlink(missing_ok=True)


def start_opening_welcome_chat_owed_from_first_boot(
    agent_manager: AgentManager, workspace_root: Path, is_stopping: threading.Event
) -> None:
    """Open the owed welcome chat on a thread of its own, since the agent list is read after the app starts."""
    threading.Thread(
        target=open_welcome_chat_owed_from_first_boot,
        args=(agent_manager, workspace_root / OWES_WELCOME_CHAT_RELATIVE_PATH, is_stopping),
        name="first-boot-welcome-chat",
        daemon=True,
    ).start()

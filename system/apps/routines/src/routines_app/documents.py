"""What the Routines app injects into its document's head: the chat app's origin label, which the pages read off
the document to link a run's chat rather than guess its host (an app's origin label is minted, not its name)."""

import html
from typing import Final

CHAT_LABEL_META_NAME: Final[str] = "routines-chat-label"


def inject_chat_label_meta_tag(html_content: str, chat_label: str) -> str:
    """Name the chat app's origin label in the document's head; "" when none is registered."""
    meta_tag = f'<meta name="{CHAT_LABEL_META_NAME}" content="{html.escape(chat_label, quote=True)}">'
    return html_content.replace("</head>", f"{meta_tag}\n</head>")

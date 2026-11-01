"""The ``account`` label a chat's agent carries, read one way everywhere (the multiplayer spec, section 7.3).

An agent chat's ``mngr create`` binds it to an account and records that in the ``account`` label, which is how
the page shows the provider, a re-auth finds the chats to restart, and a send is refused once the account signs
out. A room runs on no account by definition, whatever its labels say: the workspace's create defaults
(``.mngr/settings.local.toml``, written by the account store) stamp the default account's label onto every
create, and although the room's own create clears that layer, a room created before it did still carries the
label. Every reader of the label therefore goes through ``account_id_of``, which answers None for a room.
"""

from collections.abc import Mapping
from typing import Final

from imbue.chat.harnesses.harness_type import HarnessType
from imbue.imbue_common.pure import pure

ACCOUNT_LABEL_KEY: Final[str] = "account"


@pure
def account_id_of(harness: HarnessType, labels: Mapping[str, str]) -> str | None:
    """The account an agent runs on, as its ``account`` label names it: None for a room, and for an agent with no
    (or a blank) label, which is a chat from before accounts."""
    if harness is HarnessType.ROOM:
        return None
    account_id = labels.get(ACCOUNT_LABEL_KEY, "").strip()
    return account_id if account_id else None

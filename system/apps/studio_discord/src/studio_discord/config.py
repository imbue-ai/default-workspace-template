"""The one server and the channels this app is hardcoded to. Nothing outside these is ever read."""

from typing import Final, NamedTuple

GUILD_ID: Final = "1391837726583820409"


class Channel(NamedTuple):
    id: str
    name: str
    # "text" reads messages; "forum" reads its posts (threads).
    kind: str
    # Shown before the user connects (a channel's topic needs a signed-in read); replaced by the live topic after.
    topic: str


CHANNELS: Final = (
    Channel("1553919346458370170", "general", "text", "Chat with the team"),
    Channel("1553919372630827139", "show-and-tell", "text", "Show what you built"),
    Channel("1553919381380145203", "help-and-bugs", "forum", "Questions and bug reports"),
)
CHANNEL_IDS: Final = frozenset(channel.id for channel in CHANNELS)

# The live invite, kept in the default workspace template on its own branch so an expired invite is fixed by
# editing one file (no release). Fallbacks, in order: the server widget's own invite, then this last-known one.
INVITE_CATALOG_URL: Final = "https://raw.githubusercontent.com/imbue-ai/default-workspace-template/mngr/studio-discord/catalog/studio-discord.json"
LAST_KNOWN_INVITE: Final = "https://discord.gg/pynvT9nJm"

MESSAGES_PER_CHANNEL: Final = 3


def channel_url(channel_id: str) -> str:
    return f"https://discord.com/channels/{GUILD_ID}/{channel_id}"


def message_url(channel_id: str, message_id: str) -> str:
    return f"https://discord.com/channels/{GUILD_ID}/{channel_id}/{message_id}"

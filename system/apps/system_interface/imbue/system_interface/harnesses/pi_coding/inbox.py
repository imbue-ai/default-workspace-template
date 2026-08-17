"""The ``pi_inbox`` protocol shared across the dwt side: the shoulder-tap endpoint (server),
the queue watcher, and the stop-button override.

mngr appends outgoing messages to ``<state>/pi_inbox`` as one JSON *string* per line; the pi
lifecycle extension injects each into the live session. Two JSON *object* sentinels ride the
same ordered, append-only file (a normal message is a JSON string, so the two never collide):

* ``{"minds_interrupt": true}`` -- the shoulder-tap FLUSH: interrupt the running turn and
  resubmit the parked steers as one merged turn.
* ``{"minds_interrupt_retract": true}`` -- the stop-button RETRACT: interrupt the running turn
  and DISCARD the parked steers (Minds hands the queued messages back to the composer).

Kept in sync with the extension (mngr_pi_coding/resources/mngr_pi_lifecycle.ts), which owns the
matching ``INTERRUPT_KEY`` / ``RETRACT_KEY`` constants.
"""

import json
from pathlib import Path

# The inbox file name at the agent state-dir root. Kept in sync with INBOX_NAME in the extension.
PI_INBOX_NAME: str = "pi_inbox"
# Flush sentinel key (INTERRUPT_KEY in the extension): interrupt + resubmit the parked steers.
PI_INTERRUPT_KEY: str = "minds_interrupt"
# Retract sentinel key (RETRACT_KEY in the extension): interrupt + discard the parked steers.
PI_RETRACT_KEY: str = "minds_interrupt_retract"
# Both sentinel keys. A queue-mirror inbox line carrying either is a positional clear, not a
# message: everything before it was committed (flush) or discarded (retract) by the extension.
PI_SENTINEL_KEYS: frozenset[str] = frozenset({PI_INTERRUPT_KEY, PI_RETRACT_KEY})


def append_pi_inbox_sentinel(inbox_path: Path, key: str) -> None:
    """Append one sentinel object line ``{<key>: true}`` to the inbox, creating parents.

    Raises :class:`OSError` on a write failure (the caller maps it to a 500).
    """
    inbox_path.parent.mkdir(parents=True, exist_ok=True)
    with inbox_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({key: True}) + "\n")


def is_sentinel_object(content: object) -> bool:
    """Whether a parsed inbox line is a flush/retract sentinel object (either key ``== true``)."""
    if not isinstance(content, dict):
        return False
    return any(value is True and key in PI_SENTINEL_KEYS for key, value in content.items())

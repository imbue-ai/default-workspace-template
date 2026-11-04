"""What a labeled preview frame says the page inside it is.

Kept in the bare package so the frame code that runs under the system ``python3``, with
no venv, can import it.
"""

from enum import StrEnum


class FrameKind(StrEnum):
    """What the framed page is; its value is the wrapper's ``--kind`` and what a frame's record stores."""

    # A proposed change to an app the user already has (update-app's previews).
    CHANGE = "change"
    # A new app's look-and-feel mock (build-app), up until the app is really hooked
    # up to its data: hard-coded content or a sample, with some of it not working yet.
    MOCK = "mock"

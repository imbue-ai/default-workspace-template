"""The onboarding items: the four things worth trying in Studio, and what tells that one is done.

The chat's new-chat suggestions offer the items that are not done, and the onboarding checklist
lists all four with a check mark on the done ones. Both read the items, their order, their words
and the done rule from here, so the two never disagree.

An item is done when its marker is set (``done_marker``), except building an app, which is done
when the app registry holds an app the user built. This package cannot read the registry (it is
stdlib-only), so the caller reads it and hands the answer to ``is_item_done``.
"""

from enum import StrEnum
from pathlib import Path
from typing import assert_never

from onboarding_activity.markers import Marker, is_marked


class OnboardingItem(StrEnum):
    """Every onboarding item, in the order they are shown; the value is the item's id on the wire."""

    NEW_CHAT = "new_chat"
    PHONE_ACCESS = "phone_access"
    BUILD_APP = "build_app"
    CONNECT_DATA = "connect_data"


def item_label(item: OnboardingItem) -> str:
    """What the item says wherever it is shown."""
    match item:
        case OnboardingItem.NEW_CHAT:
            return "Work in parallel by starting a new chat"
        case OnboardingItem.PHONE_ACCESS:
            return "Access Studio from your phone"
        case OnboardingItem.BUILD_APP:
            return "Build an app"
        case OnboardingItem.CONNECT_DATA:
            return "Connect some data"
        case _ as unreachable:
            assert_never(unreachable)


def done_marker(item: OnboardingItem) -> Marker | None:
    """The marker that is set once the item is done; None for the one item the app registry
    decides instead."""
    match item:
        case OnboardingItem.NEW_CHAT:
            return Marker.SECOND_CHAT_STARTED
        case OnboardingItem.PHONE_ACCESS:
            return Marker.PHONE_LAYOUT_USED
        case OnboardingItem.BUILD_APP:
            return None
        case OnboardingItem.CONNECT_DATA:
            return Marker.PERMISSION_REQUEST_FILED
        case _ as unreachable:
            assert_never(unreachable)


def is_item_done(
    item: OnboardingItem, has_user_built_app: bool, directory: Path | None = None
) -> bool:
    """Whether the workspace's owner has done the item; raises MarkerUnreadableError when the
    item's marker could not be read, which is not the same as the item not being done.

    ``has_user_built_app`` is whether the app registry holds an app the user built, and
    ``directory`` the marker directory, by default the workspace's own."""
    marker = done_marker(item)
    if marker is None:
        return has_user_built_app
    return is_marked(marker, directory)

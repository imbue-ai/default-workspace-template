"""The checklist's items: the workspace's four onboarding items, each with whether its owner has done it.

The items, their order, their words and the rule for done are the workspace's (``onboarding_activity.items``), which
the chat's new-chat suggestions read too, so the checklist and the suggestions never disagree. An item whose state
cannot be read is listed as not done, and logged: the list is always whole.
"""

from collections.abc import Callable
from pathlib import Path

from loguru import logger

from app_manifest.errors import AppManifestError
from onboarding_activity.items import OnboardingItem
from onboarding_activity.items import done_marker
from onboarding_activity.items import is_item_done
from onboarding_activity.items import item_label
from onboarding_activity.markers import MarkerUnreadableError
from onboarding_checklist.data_types import ChecklistItem


def _is_item_known_done(item: OnboardingItem, is_app_built: bool | None, marker_directory: Path | None) -> bool:
    """Whether the owner has done the item; False too when that could not be read (logged for a marker).

    ``is_app_built`` is whether the registry holds an app the user built, None when it could not be read."""
    if is_app_built is None and done_marker(item) is None:
        # The registry alone says whether this item is done, and it could not be read.
        return False
    try:
        return is_item_done(item, is_app_built is True, marker_directory)
    except MarkerUnreadableError as error:
        logger.warning("Could not read whether the onboarding item {} is done: {}", item.value, error)
        return False


def read_checklist_items(
    read_has_user_built_app: Callable[[], bool], marker_directory: Path | None
) -> tuple[ChecklistItem, ...]:
    """Every onboarding item in order, with whether it is done as far as could be read.

    ``read_has_user_built_app`` reads whether the app registry holds an app the user built, raising
    AppManifestError when it cannot; ``marker_directory`` is the onboarding markers' directory, None for the
    workspace's own."""
    try:
        is_app_built: bool | None = read_has_user_built_app()
    except AppManifestError as error:
        logger.warning("Could not read whether the workspace holds an app its user built: {}", error)
        is_app_built = None
    return tuple(
        ChecklistItem(
            id=item.value,
            label=item_label(item),
            is_done=_is_item_known_done(item, is_app_built, marker_directory),
        )
        for item in OnboardingItem
    )

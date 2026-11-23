from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel


class HealthResponse(FrozenModel):
    """What the health probe answers."""

    status: str = Field(description="Always 'ok'")
    is_frontend_built: bool = Field(description="Whether the page's bundle has been built, so the page can be served")


class ChecklistItem(FrozenModel):
    """One onboarding item as the checklist lists it."""

    id: str = Field(description="The item's id, which a click on its entry is told apart by")
    label: str = Field(description="What the entry says")
    is_done: bool = Field(
        description="Whether the workspace's owner has done the item; false too when that could not be read"
    )


class ItemsResponse(FrozenModel):
    """What the items route answers: every onboarding item, in the order they are listed."""

    items: tuple[ChecklistItem, ...] = Field(description="The items, in order")

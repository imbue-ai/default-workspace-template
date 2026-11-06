"""What the avatar routes answer."""

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.system_interface.avatar.catalog import DesignListing


class AvatarsListing(FrozenModel):
    """The designs the workspace can show, which one is selected, and the default."""

    designs: tuple[DesignListing, ...] = Field(description="Every design in the catalog")
    selected: str = Field(description="The selected design's id")
    default: str = Field(description="The design shown when the selected one is gone")


class AvatarRegistered(FrozenModel):
    """What registering a design answers (201)."""

    id: str = Field(description="The design's catalog id")


class AvatarSelected(FrozenModel):
    """What selecting a design answers."""

    design: str = Field(description="The design every client now draws")

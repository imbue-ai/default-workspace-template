from datetime import datetime, timezone
from enum import auto

from app_manifest.primitives import ActionId
from imbue.imbue_common.enums import LowerCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from pydantic import AwareDatetime, Field, field_validator

from app_instances.primitives import (
    InstanceKey,
    InstanceTitle,
    InstanceUrl,
    LocationTarget,
)


class InstanceStatus(LowerCaseStrEnum):
    """What an instance is doing, as its app reports it (a wire value of the instances API)."""

    WORKING = auto()
    IDLE = auto()
    ATTENTION = auto()
    STOPPED = auto()
    ERROR = auto()


class InstanceLifetime(LowerCaseStrEnum):
    """How long an instance lives: until deleted, or only while a project or a client layout references it."""

    EXPLICIT = auto()
    REFERENCED = auto()


class InstanceRecord(FrozenModel):
    """One instance as the instances API lists it (contracts.md section 4.1); ``model_dump(mode="json")`` is the wire shape."""

    key: InstanceKey = Field(description="The app-scoped identifier")
    url: InstanceUrl = Field(
        description="Where the instance's page is, as a path under the app's origin"
    )
    title: InstanceTitle = Field(description="What users see")
    status: InstanceStatus = Field(description="What the instance is doing")
    lifetime: InstanceLifetime = Field(
        description="Whether the instance lives until deleted or only while referenced"
    )
    last_active: AwareDatetime | None = Field(
        description="When the instance was last active, in UTC; None when unknown"
    )
    # Named after the wire key rather than with the is_ prefix: the record is the JSON the shell reads.
    renameable: bool = Field(
        description="Whether the rename route is accepted for this instance"
    )

    @field_validator("last_active")
    @classmethod
    def _anchor_last_active_to_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value.astimezone(timezone.utc)


class CreateRequest(FrozenModel):
    """The body of ``POST /_instances``."""

    action: ActionId = Field(description="The id of a declared action")
    params: dict[str, str] = Field(
        default_factory=dict, description="The action's parameters, by name"
    )


class RenameRequest(FrozenModel):
    """The body of ``POST /_instances/<key>/rename``."""

    title: InstanceTitle = Field(description="The new title")


class LocationRequest(FrozenModel):
    """The body of ``POST /_instances/<key>/location``."""

    path: LocationTarget = Field(
        description="Where the instance's page is now: a rooted path, or an absolute URL for an app that navigates to one"
    )

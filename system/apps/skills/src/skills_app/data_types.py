from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel


class HealthResponse(FrozenModel):
    """What the health probe answers."""

    status: str = Field(description="Always 'ok'")

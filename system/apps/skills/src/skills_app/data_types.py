from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel


class HealthResponse(FrozenModel):
    """What the health probe answers."""

    status: str = Field(description="Always 'ok'")


class RenderedInstructions(FrozenModel):
    """A skill's instructions as its page shows them."""

    html: str = Field(
        description="Its SKILL.md's body rendered from markdown, raw HTML escaped and links to its files as file: URLs"
    )

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel


class HealthResponse(FrozenModel):
    """What the health probe answers."""

    status: str = Field(description="Always 'ok'")
    is_frontend_built: bool = Field(description="Whether the pages' bundle has been built, so a page can be served")


class EnabledRequest(FrozenModel):
    """The body of the switch route."""

    is_enabled: bool = Field(description="Whether the routine should run on its schedule")


class FixChatResponse(FrozenModel):
    """What the fix route answers: the chat it opened."""

    chat_id: str = Field(description="The chat's id (the route answers 502 when none could be read)")

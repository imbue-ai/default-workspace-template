from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel


class HealthResponse(FrozenModel):
    """What the health probe answers."""

    status: str = Field(description="Always 'ok'")
    is_frontend_built: bool = Field(description="Whether the pages' bundle has been built, so a page can be served")


class AppNamesResponse(FrozenModel):
    """The display name of every registered app, which the pages show an app's usage under."""

    display_names: dict[str, str] = Field(description="Display name by registered app name")


class SourceNamesResponse(FrozenModel):
    """The titles the pages show a chat's or a routine's usage under; an id missing here is shown as itself."""

    chats: dict[str, str] = Field(description="Chat title by chat id, from the chat app")
    routines: dict[str, str] = Field(description="Routine title by routine id, from its definition file")

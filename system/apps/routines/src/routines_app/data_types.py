from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from routines.data_types import RoutineDetail
from routines.data_types import RoutineListing
from routines.data_types import RoutineRow


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


class RoutineRowWithOwner(RoutineRow):
    """A routine as the list shows it, with the app its definition file belongs to and what that leaves the user
    able to change from this app."""

    owning_app: str | None = Field(
        description="The app whose directory holds the definition file, by the name the workspace's record of the "
        "app gives it, else by the directory's own name; None for a routine under system/routines"
    )
    is_schedule_only: bool = Field(
        description="Whether the schedule and the on/off switch are all that can be changed here: the definition "
        "belongs to an app, so editing anything else or removing it is a change to that app"
    )
    has_schedule_override: bool = Field(
        description="Whether the routine runs on a schedule the user gave it in place of the one its app gives it"
    )
    app_schedule_text: str | None = Field(
        description="The app's own schedule in words while the user's is in its place; None otherwise"
    )


class RoutineListingWithOwners(RoutineListing):
    """Every routine, each with its owning app, and the summary strip."""

    routines: tuple[RoutineRowWithOwner, ...] = Field(description="The rows, problems first")


class RoutineDetailWithOwner(RoutineDetail):
    """One routine with its owning app, its definition and recent runs."""

    row: RoutineRowWithOwner = Field(description="The routine as the list shows it")

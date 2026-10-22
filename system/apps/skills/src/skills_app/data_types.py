from enum import auto

from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel


class HealthResponse(FrozenModel):
    """What the health probe answers."""

    status: str = Field(description="Always 'ok'")


class RenderedInstructions(FrozenModel):
    """A skill's instructions as its page shows them."""

    html: str = Field(
        description="Its SKILL.md's body rendered from markdown, raw HTML escaped and links to its files as file: URLs"
    )


class OnlineSkillState(UpperCaseStrEnum):
    """How a skill found online stands against the workspace's own."""

    # The workspace has no skill of its name.
    AVAILABLE = auto()
    # The workspace has it: a skill of its name, recorded as installed from the same source.
    INSTALLED = auto()
    # The workspace has a different skill of its name, which installing this one would land on.
    NAME_TAKEN = auto()


class OnlineSkill(FrozenModel):
    """One skill the online index matched, as the Find skills page shows it."""

    name: str = Field(description="Its name, which the skills CLI installs it by")
    source: str = Field(description="Where it is published: a GitHub owner/repo, or a host")
    installs: int = Field(description="How many times the skills CLI has installed it")
    page_url: str = Field(description="Its page on skills.sh, which shows what it does and its instructions")
    install_command: str = Field(description="The skills CLI command that installs it into this workspace")
    state: OnlineSkillState = Field(description="Whether the workspace has it, or a different skill of its name")


class OnlineSkillResults(FrozenModel):
    """What a search of the online index found, most installed first."""

    query: str = Field(description="What was searched for")
    skills: tuple[OnlineSkill, ...] = Field(description="The matches, most installed first")

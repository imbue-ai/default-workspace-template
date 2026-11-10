from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel


class Fact(FrozenModel):
    """One remembered fact: one line of the profile memory that starts with ``- ``."""

    id: str = Field(
        description="Names this exact line: its index in the file and a hash of its bytes, so a stale page cannot "
        "delete another line"
    )
    text: str = Field(description="The fact, or the whole line as written when it is not in the fixed shape")
    source_title: str | None = Field(description="The title of the page the fact came from, for a fact from the web")
    source_url: str | None = Field(
        description="The http(s) link the fact came from; None for a fact the user shared or a line not in the shape"
    )
    is_told_by_user: bool = Field(description="Whether the user shared this fact themselves")
    found_on: str | None = Field(description="The date the fact was found or confirmed, YYYY-MM-DD")
    is_parsed: bool = Field(description="Whether the line is in the fixed shape the lookup writes")


class ProfileResponse(FrozenModel):
    """What is remembered from the lookup: each fact, and the raw files it was read from."""

    facts: tuple[Fact, ...] = Field(description="Every fact in the profile memory, in file order")
    profile_markdown: str | None = Field(description="The profile memory file as it is on disk; None when missing")
    sources_json: str | None = Field(description="The lookup's sources file as it is on disk; None when missing")


class DeleteFactRequest(FrozenModel):
    """Which fact to forget."""

    fact_id: str = Field(description="The fact's id, as the profile listed it")


class AddFactRequest(FrozenModel):
    """A fact the user tells the page about themselves."""

    text: str = Field(description="The fact, in the user's words; one line, at most 500 characters")


class EditFactRequest(FrozenModel):
    """New words for one fact, keeping its source and date."""

    fact_id: str = Field(description="The fact's id, as the profile listed it")
    text: str = Field(description="The fact's new words; one line, at most 500 characters")


class HealthResponse(FrozenModel):
    """What the health probe answers."""

    status: str = Field(description="Always 'ok'")

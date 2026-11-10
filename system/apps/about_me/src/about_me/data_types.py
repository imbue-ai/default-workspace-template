from typing import Literal

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


MemoryType = Literal["user", "feedback", "project", "reference", "other"]


class Memory(FrozenModel):
    """One of the agent's memories: one file in the memory directory."""

    file_name: str = Field(description="The memory's file name in data/memories/")
    revision: str = Field(description="A hash of the file's bytes, so a stale page cannot change a newer version")
    title: str = Field(description="The memory's title in MEMORY.md, or its name as words when the index has none")
    description: str | None = Field(description="The memory's one-line summary from its frontmatter")
    memory_type: MemoryType = Field(
        description="What the memory is about: the user, how they like to work, their projects, a pointer, or other"
    )
    body: str = Field(description="The memory's text after its frontmatter")


class ProfileResponse(FrozenModel):
    """Everything the agent remembers: each memory, the web lookup's facts, and the raw files they come from."""

    memories: tuple[Memory, ...] = Field(description="Every memory except the web lookup's, by file name")
    profile_file_name: str = Field(description="The file name of the web lookup's memory")
    profile_revision: str | None = Field(description="A hash of the web lookup's memory; None when it is missing")
    memory_index_markdown: str | None = Field(description="MEMORY.md as it is on disk; None when missing")
    facts: tuple[Fact, ...] = Field(description="Every fact in the web lookup's memory, in file order")
    profile_markdown: str | None = Field(description="The profile memory file as it is on disk; None when missing")
    sources_json: str | None = Field(description="The lookup's sources file as it is on disk; None when missing")


class DeleteFactRequest(FrozenModel):
    """Which fact to forget."""

    fact_id: str = Field(description="The fact's id, as the profile listed it")


class AddMemoryRequest(FrozenModel):
    """Something the user tells the page about themselves, kept as a new memory."""

    text: str = Field(description="What to remember, in the user's words; one line, at most 500 characters")


class EditMemoryRequest(FrozenModel):
    """A memory's new summary and text."""

    file_name: str = Field(description="The memory's file name, as the page listed it")
    revision: str = Field(description="The memory's revision, as the page listed it")
    description: str = Field(description="The new one-line summary; at most 300 characters")
    body: str = Field(description="The new text; at most 5000 characters")


class DeleteMemoryRequest(FrozenModel):
    """Which memory to delete."""

    file_name: str = Field(description="The memory's file name, as the page listed it")
    revision: str = Field(description="The memory's revision, as the page listed it")


class ForgetRequest(FrozenModel):
    """How much to forget."""

    scope: Literal["lookup", "everything"] = Field(
        description="'lookup' forgets what the web lookup found; 'everything' deletes every memory"
    )


class EditFactRequest(FrozenModel):
    """New words for one fact, keeping its source and date."""

    fact_id: str = Field(description="The fact's id, as the profile listed it")
    text: str = Field(description="The fact's new words; one line, at most 500 characters")


class HealthResponse(FrozenModel):
    """What the health probe answers."""

    status: str = Field(description="Always 'ok'")

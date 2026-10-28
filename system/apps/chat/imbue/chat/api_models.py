"""The answer models of the chat's routes that no other module owns: what ``GET /api/harnesses`` answers."""

from pydantic import Field
from pydantic import RootModel

from imbue.chat.harnesses.model import HarnessCatalog
from imbue.chat.harnesses.registry import HarnessPopup


class HarnessCatalogEntry(HarnessCatalog):
    """One harness's catalog as ``GET /api/harnesses`` answers it: the catalog plus the spec's declarations."""

    popups: tuple[HarnessPopup, ...] = Field(description="The popups the harness declares for the chat page")
    supports_compaction: bool = Field(description="Whether the harness can compact a conversation")
    can_interrupt_compaction: bool = Field(description="Whether a compaction in flight can be interrupted")
    label: str = Field(description="The harness's user-facing name")


class HarnessCatalogsResponse(RootModel[dict[str, HarnessCatalogEntry]]):
    """What ``GET /api/harnesses`` answers: every harness's catalog, keyed by the harness's id."""

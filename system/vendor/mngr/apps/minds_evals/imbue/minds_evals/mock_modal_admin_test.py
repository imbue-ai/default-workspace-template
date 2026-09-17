from pydantic import Field

from imbue.minds_evals.cleanup_environments import ModalEnvironmentAdminInterface
from imbue.minds_evals.data_types import ModalDeletionOutcome


class MockModalEnvironmentAdmin(ModalEnvironmentAdminInterface):
    """An in-memory Modal workspace: a set of environment names that deletion actually removes.

    Deleting a name that is not there answers NOT_FOUND, which is how a real workspace answers a
    second cleanup pass over the same run.
    """

    environment_names: list[str] = Field(default_factory=list, description="Environments the workspace holds")
    undeletable_names: frozenset[str] = Field(
        default_factory=frozenset, description="Environments whose deletion Modal refuses"
    )
    deletion_attempts: list[str] = Field(
        default_factory=list, description="Every name delete_environment was called with, in order"
    )
    listing_count: int = Field(default=0, description="How many times the workspace was listed")

    def list_environment_names(self) -> tuple[str, ...]:
        self.listing_count += 1
        return tuple(self.environment_names)

    def delete_environment(self, environment_name: str) -> ModalDeletionOutcome:
        self.deletion_attempts.append(environment_name)
        if environment_name in self.undeletable_names:
            return ModalDeletionOutcome.FAILED
        if environment_name not in self.environment_names:
            return ModalDeletionOutcome.NOT_FOUND
        self.environment_names.remove(environment_name)
        return ModalDeletionOutcome.DELETED

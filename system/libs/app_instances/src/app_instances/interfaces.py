from abc import ABC, abstractmethod
from collections.abc import Mapping

from app_manifest.primitives import ActionId
from imbue.imbue_common.mutable_model import MutableModel

from app_instances.data_types import InstanceRecord
from app_instances.primitives import InstanceKey, InstanceTitle, LocationTarget


class InstanceSourceInterface(MutableModel, ABC):
    """What an app knows about its instances; the blueprint serves one of these over the instances API.

    Every method may raise NotReadyError while the app is initialising. Implementations must be
    safe to call from several threads at once: the API is served by a threaded server.
    """

    @abstractmethod
    def list_instances(self) -> list[InstanceRecord]:
        """Every instance the app currently has, in list order."""

    @abstractmethod
    def create_instance(
        self, action: ActionId, params: Mapping[str, str]
    ) -> InstanceRecord:
        """Create one instance through a declared action; raises UnknownActionError, InvalidParamsError, or InstanceConflictError."""

    @abstractmethod
    def delete_instance(self, key: InstanceKey) -> None:
        """Destroy the instance and whatever it owns; an unknown key is not an error."""

    @abstractmethod
    def rename_instance(self, key: InstanceKey, title: InstanceTitle) -> InstanceRecord:
        """Retitle the instance; raises NotRenameableError, UnknownInstanceError, or InstanceConflictError (a title collision)."""

    @abstractmethod
    def set_location(self, key: InstanceKey, path: LocationTarget) -> InstanceRecord:
        """Record where the instance's page now is, or navigate it there; raises LocationNotTrackedError, InvalidInstanceValueError (a form of location this app does not take), UnknownInstanceError, or InstanceConflictError (the app cannot navigate there right now)."""


class InstanceNudgerInterface(MutableModel, ABC):
    """Tells the shell that this app's instance list changed."""

    @abstractmethod
    def nudge(self) -> None:
        """Ask the shell to refetch this app's list; must never raise for an unreachable shell."""

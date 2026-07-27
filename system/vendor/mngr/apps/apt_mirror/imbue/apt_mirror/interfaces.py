from abc import ABC
from abc import abstractmethod

from imbue.imbue_common.mutable_model import MutableModel


class AptMirrorStorageInterface(MutableModel, ABC):
    """Object storage the mirror reads and writes (R2 in production)."""

    @abstractmethod
    def get_object(self, key: str) -> bytes | None:
        """Return the object's bytes, or None when the key does not exist."""

    @abstractmethod
    def put_object(self, key: str, data: bytes) -> None:
        """Store the object, overwriting any existing content."""

    @abstractmethod
    def has_object(self, key: str) -> bool:
        """Return whether the key exists without fetching its content."""


class UpstreamFetcherInterface(MutableModel, ABC):
    """HTTP fetcher for the Debian archives."""

    @abstractmethod
    def fetch(self, url: str) -> bytes | None:
        """Return the response body, or None on a definitive 404."""

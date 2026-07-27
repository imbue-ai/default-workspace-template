"""In-memory mock implementations of the apt mirror interfaces."""

from pydantic import Field

from imbue.apt_mirror.interfaces import AptMirrorStorageInterface
from imbue.apt_mirror.interfaces import UpstreamFetcherInterface


class InMemoryAptMirrorStorage(AptMirrorStorageInterface):
    """Dict-backed storage for tests."""

    objects_by_key: dict[str, bytes] = Field(default_factory=dict, description="Stored objects")
    put_count: int = Field(default=0, description="Number of put_object calls")

    def get_object(self, key: str) -> bytes | None:
        return self.objects_by_key.get(key)

    def put_object(self, key: str, data: bytes) -> None:
        self.objects_by_key[key] = data
        self.put_count = self.put_count + 1

    def has_object(self, key: str) -> bool:
        return key in self.objects_by_key


class MappingUpstreamFetcher(UpstreamFetcherInterface):
    """URL->bytes mapping fetcher for tests; unknown URLs answer 404 (None)."""

    responses_by_url: dict[str, bytes] = Field(default_factory=dict, description="Canned responses")
    fetched_urls: list[str] = Field(default_factory=list, description="Every URL fetched, in order")

    def fetch(self, url: str) -> bytes | None:
        self.fetched_urls.append(url)
        return self.responses_by_url.get(url)

import httpx
from pydantic import ConfigDict
from pydantic import Field
from tenacity import retry
from tenacity import retry_if_exception_type
from tenacity import stop_after_attempt
from tenacity import wait_exponential

from imbue.apt_mirror.errors import AptMirrorTransientUpstreamError
from imbue.apt_mirror.errors import AptMirrorUpstreamError
from imbue.apt_mirror.interfaces import UpstreamFetcherInterface


class HttpUpstreamFetcher(UpstreamFetcherInterface):
    """httpx-backed fetcher with retry/backoff on transient upstream failures.

    snapshot.debian.org in particular throttles with 503s; retries with
    exponential backoff ride those out, and a definitive 404 maps to None so
    callers can fall through to the next upstream. httpx.Client is thread-safe,
    so one instance serves the warm pool.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    client: httpx.Client = Field(frozen=True, description="HTTP client used for upstream fetches")

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, AptMirrorTransientUpstreamError)),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=1, max=15),
        reraise=True,
    )
    def fetch(self, url: str) -> bytes | None:
        response = self.client.get(url, follow_redirects=True)
        if response.status_code == 404:
            return None
        if response.status_code >= 500 or response.status_code == httpx.codes.TOO_MANY_REQUESTS:
            # Raised (and retried) rather than returned: a throttled upstream
            # must not masquerade as a missing file.
            raise AptMirrorTransientUpstreamError(url, response.status_code)
        if not response.is_success:
            # Any other unexpected status is fatal but still an AptMirrorError,
            # so the CLI reports it as a clean one-line error.
            raise AptMirrorUpstreamError(url, response.status_code)
        return response.content

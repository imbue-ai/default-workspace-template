from collections.abc import Sequence
from typing import Any

from browser.cdp_client import CdpClient, CdpError

# A loopback endpoint nothing listens on: the fake never connects, so it is never dialled.
_UNUSED_ENDPOINT = "http://127.0.0.1:0"


class NavigatingCdpClient(CdpClient):
    """A CdpClient with no socket: answers the tabs it was built with and records every navigation."""

    def __init__(
        self, targets: Sequence[dict[str, Any]], navigation_failure: str | None
    ) -> None:
        super().__init__(_UNUSED_ENDPOINT)
        self.targets = list(targets)
        # When set, every navigation raises CdpError with this text (what Chromium's
        # ``Page.navigate`` errorText comes back as).
        self.navigation_failure = navigation_failure
        self.navigations: list[tuple[str, str]] = []

    async def page_targets(self) -> list[dict[str, Any]]:
        return list(self.targets)

    async def navigate(self, target_id: str, url: str) -> None:
        if self.navigation_failure is not None:
            raise CdpError(f"Page.navigate to {url}: {self.navigation_failure}")
        self.navigations.append((target_id, url))
        self.targets = [
            {**target, "url": url} if target["targetId"] == target_id else target
            for target in self.targets
        ]

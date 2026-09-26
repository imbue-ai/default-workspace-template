from collections.abc import Sequence
from typing import Any

from browser.cdp_client import CdpClient, CdpError

# A loopback endpoint nothing listens on: the fake never connects, so it is never dialled.
_UNUSED_ENDPOINT = "http://127.0.0.1:0"


class RecordingCdpClient(CdpClient):
    """A CdpClient with no socket: records every protocol frame and answers attaches with one session."""

    def __init__(self, failing_method: str | None) -> None:
        super().__init__(_UNUSED_ENDPOINT)
        # When set, a send of this method raises CdpError, as Chromium's refusal would.
        self.failing_method = failing_method
        self.frames: list[tuple[str, dict[str, Any], str | None]] = []

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        self.frames.append((method, dict(params or {}), session_id))
        if method == self.failing_method:
            raise CdpError(f"{method}: refused")
        if method == "Target.attachToTarget":
            return {"sessionId": "session-1"}
        return {}


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


class TabClosingCdpClient(NavigatingCdpClient):
    """Answers targets and records the tabs the fleet creates, closes, and foregrounds."""

    def __init__(self, targets: Sequence[dict[str, Any]]) -> None:
        super().__init__(targets, navigation_failure=None)
        self.created: list[str] = []
        self.closed: list[str] = []
        self.activated: list[str] = []

    async def create_target(self, url: str) -> str:
        target_id = f"new-{len(self.created) + 1}"
        self.created.append(url)
        self.targets.append({"targetId": target_id, "url": url})
        return target_id

    async def close_target(self, target_id: str) -> None:
        self.closed.append(target_id)
        self.targets = [t for t in self.targets if t["targetId"] != target_id]

    async def activate(self, target_id: str) -> None:
        self.activated.append(target_id)

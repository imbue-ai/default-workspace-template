"""Concrete in-memory double for the third-party ``ovh.Client``.

Subclasses the real client so it passes ``OvhPublicCloudRelayProvisioner``'s
``ovh.Client`` field validation, but answers every call from canned responses
and records mutations instead of talking to the OVH API.
"""

from typing import Any

import ovh


class FakeOvhClient(ovh.Client):
    """``ovh.Client`` double: canned GET responses, recorded POST/DELETE calls."""

    def __init__(self, get_responses_by_url: dict[str, Any] | None = None) -> None:
        super().__init__(
            endpoint="ovh-us",
            application_key="fake-app-key",
            application_secret="fake-app-secret",
            consumer_key="fake-consumer-key",
        )
        self.get_responses_by_url: dict[str, Any] = dict(get_responses_by_url or {})
        self.post_calls: list[tuple[str, dict[str, Any]]] = []
        self.post_response: dict[str, Any] = {"id": "created-id"}
        self.deleted_urls: list[str] = []

    def get(self, _target: str, _need_auth: bool = True, **kwargs: Any) -> Any:
        return self.get_responses_by_url[_target]

    def post(self, _target: str, _need_auth: bool = True, **kwargs: Any) -> Any:
        self.post_calls.append((_target, kwargs))
        return self.post_response

    def delete(self, _target: str, _need_auth: bool = True, **kwargs: Any) -> Any:
        self.deleted_urls.append(_target)
        return None

"""A local HTTP server the chat app and notifier adapters are pointed at in tests."""

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel


class RecordedRequest(FrozenModel):
    """One request the fake server took."""

    method: str = Field(description="The HTTP method")
    path: str = Field(description="The path with its query")
    headers: dict[str, str] = Field(description="The headers, lower-cased")
    body: dict[str, object] | None = Field(description="The JSON body; None when there was none")


class FakeHttpServer(MutableModel):
    """A local server answering each (method, path) from a script, in order, and keeping what it was asked."""

    url: str = Field(frozen=True, description="Where it listens")
    answers: dict[tuple[str, str], list[tuple[int, object]]] = Field(
        default_factory=dict, description="The (status, JSON body) answers per route; the last repeats"
    )
    requests: list[RecordedRequest] = Field(default_factory=list, description="Every request, in order")

    def answer(self, method: str, path: str, *responses: tuple[int, object]) -> None:
        self.answers[(method, path)] = list(responses)

    def take(
        self, method: str, path: str, headers: dict[str, str], body: dict[str, object] | None
    ) -> tuple[int, object]:
        self.requests.append(RecordedRequest(method=method, path=path, headers=headers, body=body))
        scripted = self.answers.get((method, path))
        if not scripted:
            return 404, {"detail": f"no answer scripted for {method} {path}"}
        return scripted.pop(0) if len(scripted) > 1 else scripted[0]

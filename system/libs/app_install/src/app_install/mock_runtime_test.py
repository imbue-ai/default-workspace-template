from collections.abc import Sequence

from pydantic import Field

from app_install.interfaces import WorkspaceRuntimeInterface


class RecordingRuntime(WorkspaceRuntimeInterface):
    """A workspace runtime that does nothing outside the tree and records what it was asked to do."""

    calls: list[str] = Field(default_factory=list, description="Every call, in order, as one line each")
    reload_failure: str | None = Field(default=None, description="What reloading programs fails with, if it does")
    registry_failure: str | None = Field(default=None, description="What removing a row fails with, if it does")
    commit_failure: str | None = Field(default=None, description="What a commit fails with, if it does")

    def reload_programs(self) -> str | None:
        self.calls.append("reload_programs")
        return self.reload_failure

    def remove_registry_row(self, name: str) -> str | None:
        self.calls.append(f"remove_registry_row {name}")
        return self.registry_failure

    def commit_paths(self, relative_paths: Sequence[str], message: str) -> str | None:
        self.calls.append(f"commit_paths {sorted(relative_paths)} {message!r}")
        return self.commit_failure

    def delete_ref(self, ref: str) -> None:
        self.calls.append(f"delete_ref {ref}")

from pydantic import Field

from imbue.observability.openobserve_api import OpenObserveApiInterface


class MockOpenObserveApi(OpenObserveApiInterface):
    """In-memory OpenObserve API for exercising the provisioning decision logic."""

    user_emails: list[str] = Field(default_factory=list, description="Existing organization users")
    existing_stream_names: list[str] = Field(
        default_factory=list, description="Streams that exist (i.e. have received data)"
    )
    created_users: list[tuple[str, str, str]] = Field(
        default_factory=list, description="(email, password, role) tuples recorded by create_user"
    )
    retention_updates: list[tuple[str, str, int]] = Field(
        default_factory=list, description="(stream, type, days) tuples recorded by update_stream_retention"
    )

    def list_user_emails(self) -> list[str]:
        return list(self.user_emails)

    def create_user(self, email: str, password: str, role: str) -> None:
        self.created_users.append((email, password, role))
        self.user_emails.append(email)

    def update_stream_retention(self, stream_name: str, stream_type: str, retention_days: int) -> bool:
        self.retention_updates.append((stream_name, stream_type, retention_days))
        return stream_name in self.existing_stream_names

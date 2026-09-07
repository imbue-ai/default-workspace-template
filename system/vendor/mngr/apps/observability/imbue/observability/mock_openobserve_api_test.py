from collections.abc import Mapping

from pydantic import Field

from imbue.observability.data_types import DashboardSummary
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
    dashboard_summaries: list[DashboardSummary] = Field(
        default_factory=list, description="Existing dashboards, as the listing reports them"
    )
    created_dashboards: list[dict[str, object]] = Field(
        default_factory=list, description="Definitions recorded by create_dashboard"
    )
    deleted_dashboard_ids: list[str] = Field(
        default_factory=list, description="Ids recorded by delete_dashboard, in call order"
    )

    def list_user_emails(self) -> list[str]:
        return list(self.user_emails)

    def create_user(self, email: str, password: str, role: str) -> None:
        self.created_users.append((email, password, role))
        self.user_emails.append(email)

    def update_stream_retention(self, stream_name: str, stream_type: str, retention_days: int) -> bool:
        self.retention_updates.append((stream_name, stream_type, retention_days))
        return stream_name in self.existing_stream_names

    def list_dashboard_summaries(self) -> list[DashboardSummary]:
        return list(self.dashboard_summaries)

    def create_dashboard(self, definition: Mapping[str, object]) -> None:
        self.created_dashboards.append(dict(definition))

    def delete_dashboard(self, dashboard_id: str) -> None:
        self.deleted_dashboard_ids.append(dashboard_id)
        self.dashboard_summaries = [
            summary for summary in self.dashboard_summaries if summary.dashboard_id != dashboard_id
        ]

"""OpenObserve REST API access for provisioning: sender users and stream retention.

Provisioning is idempotent get-or-create: sender users whose Vault credential
already exists are left alone (their password is not recoverable and does not
need to be), only missing ones are minted. Retention overrides are re-applied
on every pass -- streams only exist once data has arrived, so an override that
finds no stream is reported as skipped rather than failed.

The HTTP endpoint shapes (user CRUD, stream settings) are the API surface the
spec's prototype validation plan pins down; they live behind
:class:`OpenObserveApiInterface` so the decision logic is unit-tested against
a mock implementation either way.
"""

import base64
import secrets
import string
from abc import ABC
from abc import abstractmethod
from collections.abc import Mapping
from typing import Final

import httpx
from loguru import logger
from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.observability.data_types import DashboardSummary
from imbue.observability.data_types import SenderCredential
from imbue.observability.errors import ObservabilityError
from imbue.observability.primitives import ALL_LOG_STREAM_NAMES
from imbue.observability.primitives import OPENOBSERVE_ORGANIZATION
from imbue.observability.primitives import SenderClass

_API_REQUEST_TIMEOUT_SECONDS: Final[float] = 30.0

# The org role minted sender users get. The pinned OSS release accepts
# exactly two roles ("Custom roles not allowed" for everything else):
# "admin" and "service_account" -- validated live on v0.92.2 during the dev
# bring-up. service_account is the least-privileged ingest identity, and its
# Basic email:password credential ingests OTLP successfully.
_SENDER_USER_ROLE: Final[str] = "service_account"

_GENERATED_PASSWORD_BYTES: Final[int] = 24

# OpenObserve rejects passwords missing any of these character classes
# ("Password must be 8-128 characters and contain at least one lowercase
# letter, one uppercase letter, one digit, and one special character" --
# observed on v0.92.2 during the dev bring-up). token_urlsafe alone cannot
# guarantee every class, so one character from each is appended.
_PASSWORD_SPECIAL_CHARACTERS: Final[str] = "!@#$%^&*"


def _generate_sender_password() -> str:
    """A random password satisfying OpenObserve's complexity policy (lower + upper + digit + special)."""
    required_class_characters = (
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.digits),
        secrets.choice(_PASSWORD_SPECIAL_CHARACTERS),
    )
    return secrets.token_urlsafe(_GENERATED_PASSWORD_BYTES) + "".join(required_class_characters)


class OpenObserveApiError(ObservabilityError):
    """Raised when an OpenObserve API call fails."""


class OpenObserveApiInterface(MutableModel, ABC):
    """Contract for the few OpenObserve API operations provisioning needs."""

    @abstractmethod
    def list_user_emails(self) -> list[str]:
        """Return the email of every user in the organization."""

    @abstractmethod
    def create_user(self, email: str, password: str, role: str) -> None:
        """Create one organization user with the given password and org role."""

    @abstractmethod
    def update_stream_retention(self, stream_name: str, stream_type: str, retention_days: int) -> bool:
        """Set one stream's data retention in days; False when the stream does not exist yet."""

    @abstractmethod
    def list_dashboard_summaries(self) -> list[DashboardSummary]:
        """Return every dashboard's id and title in the organization's default folder."""

    @abstractmethod
    def create_dashboard(self, definition: Mapping[str, object]) -> None:
        """Create one dashboard from its full definition document."""

    @abstractmethod
    def delete_dashboard(self, dashboard_id: str) -> None:
        """Delete one dashboard by its server-assigned id."""


class OpenObserveHttpApi(OpenObserveApiInterface):
    """httpx-backed implementation against one instance's API (root-authenticated)."""

    base_url: str = Field(frozen=True, description="Instance base URL, e.g. http://127.0.0.1:5080 via an SSH tunnel")
    root_user_email: str = Field(frozen=True, description="Root account email used to authenticate the calls")
    root_user_password: SecretStr = Field(frozen=True, description="Root account password")

    def _request(self, method: str, path: str, json_body: dict[str, object] | None) -> httpx.Response:
        url = f"{self.base_url.rstrip('/')}{path}"
        try:
            response = httpx.request(
                method,
                url,
                json=json_body,
                auth=(self.root_user_email, self.root_user_password.get_secret_value()),
                timeout=_API_REQUEST_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            raise OpenObserveApiError(f"OpenObserve API request {method} {path} failed: {exc}") from exc
        return response

    def list_user_emails(self) -> list[str]:
        response = self._request("GET", f"/api/{OPENOBSERVE_ORGANIZATION}/users", None)
        if response.status_code != 200:
            raise OpenObserveApiError(f"Listing users failed ({response.status_code}): {response.text}")
        payload = response.json()
        users = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(users, list):
            # A shape drift (non-dict payload, or no "data" list inside it)
            # must fail loudly here: an empty result would silently disable
            # the orphaned-user guard in ensure_sender_credentials.
            raise OpenObserveApiError(f"Unexpected users listing payload shape (no 'data' list): {payload!r}")
        return [str(user["email"]) for user in users if isinstance(user, dict) and "email" in user]

    def create_user(self, email: str, password: str, role: str) -> None:
        body: dict[str, object] = {
            "email": email,
            "password": password,
            "role": role,
            "first_name": "ingest",
            "last_name": email.split("@")[0],
        }
        response = self._request("POST", f"/api/{OPENOBSERVE_ORGANIZATION}/users", body)
        if response.status_code != 200:
            raise OpenObserveApiError(f"Creating user {email} failed ({response.status_code}): {response.text}")

    def update_stream_retention(self, stream_name: str, stream_type: str, retention_days: int) -> bool:
        path = f"/api/{OPENOBSERVE_ORGANIZATION}/streams/{stream_name}/settings?type={stream_type}"
        response = self._request("PUT", path, {"data_retention": retention_days})
        if response.status_code == 404:
            return False
        if response.status_code != 200:
            raise OpenObserveApiError(
                f"Setting retention on stream {stream_name} failed ({response.status_code}): {response.text}"
            )
        return True

    def list_dashboard_summaries(self) -> list[DashboardSummary]:
        response = self._request("GET", f"/api/{OPENOBSERVE_ORGANIZATION}/dashboards", None)
        if response.status_code != 200:
            raise OpenObserveApiError(f"Listing dashboards failed ({response.status_code}): {response.text}")
        payload = response.json()
        entries = payload.get("dashboards") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            # A shape drift must fail loudly: an empty result would make the
            # import create a duplicate beside every existing dashboard.
            raise OpenObserveApiError(
                f"Unexpected dashboards listing payload shape (no 'dashboards' list): {payload!r}"
            )
        for entry in entries:
            # Same loud-failure policy per entry: a silently dropped entry
            # would make the import duplicate that one dashboard.
            if not isinstance(entry, dict):
                raise OpenObserveApiError(f"Unexpected dashboards listing entry shape (not an object): {entry!r}")
        return [_dashboard_summary_from_listing_entry(entry) for entry in entries]

    def create_dashboard(self, definition: Mapping[str, object]) -> None:
        response = self._request("POST", f"/api/{OPENOBSERVE_ORGANIZATION}/dashboards", dict(definition))
        if response.status_code != 200:
            raise OpenObserveApiError(
                f"Creating dashboard {definition.get('title')!r} failed ({response.status_code}): {response.text}"
            )

    def delete_dashboard(self, dashboard_id: str) -> None:
        response = self._request("DELETE", f"/api/{OPENOBSERVE_ORGANIZATION}/dashboards/{dashboard_id}", None)
        if response.status_code != 200:
            raise OpenObserveApiError(
                f"Deleting dashboard {dashboard_id} failed ({response.status_code}): {response.text}"
            )


def _dashboard_summary_from_listing_entry(entry: Mapping[str, object]) -> DashboardSummary:
    """Extract one listing entry's id + title, tolerating the schema-version nesting.

    The listing wraps each dashboard under its schema-version key (``"v5":
    {...}`` on the pinned release); older releases returned the document
    flat. Read whichever nested document is present, falling back to the
    entry itself, so a version bump changes at most the nesting key.
    """
    document: Mapping[str, object] = entry
    for key, value in entry.items():
        if key.startswith("v") and key[1:].isdigit() and isinstance(value, Mapping):
            # Re-key the nested mapping so its (unverified) key type never
            # leaks into the str-keyed reads below.
            document = {str(nested_key): nested_value for nested_key, nested_value in value.items()}
            break
    return DashboardSummary(
        dashboard_id=str(document.get("dashboardId", "")),
        title=str(document.get("title", "")),
    )


@pure
def build_basic_authorization_header(email: str, password: str) -> str:
    """The complete Authorization header value senders present on every ingest request."""
    encoded = base64.b64encode(f"{email}:{password}".encode()).decode()
    return f"Basic {encoded}"


@pure
def sender_email(sender_class: SenderClass) -> str:
    return f"ingest-{str(sender_class).lower()}@imbue.com"


def ensure_sender_credentials(
    api: OpenObserveApiInterface,
    # The tier Vault entry's current INGEST_CREDENTIAL_* values (empty string
    # for a sender that has never been minted); existing values are preserved
    # verbatim so re-provisioning never rotates a credential behind the fleet.
    existing_credential_by_sender: Mapping[SenderClass, str],
) -> dict[SenderClass, SenderCredential]:
    """Get-or-create the per-sender-class ingest users, returning every sender's credential."""
    existing_emails = set(api.list_user_emails())
    credential_by_sender: dict[SenderClass, SenderCredential] = {}
    for sender_class in SenderClass:
        email = sender_email(sender_class)
        existing_credential = existing_credential_by_sender.get(sender_class, "")
        if existing_credential:
            credential_by_sender[sender_class] = SenderCredential(
                sender_email=email,
                authorization_header_value=SecretStr(existing_credential),
                is_newly_minted=False,
            )
            continue
        if email in existing_emails:
            # The user exists but Vault lost its credential: minting a fresh
            # password would require a password-reset flow we deliberately do
            # not automate. Fail loudly so the operator resolves it (delete
            # the user via the UI over an SSH tunnel, then re-run).
            raise OpenObserveApiError(
                f"Sender user {email} exists but the tier Vault entry has no credential for it; "
                "delete the user (SSH tunnel + UI) and re-run provisioning to mint a fresh one."
            )
        password = _generate_sender_password()
        api.create_user(email, password, _SENDER_USER_ROLE)
        logger.info("Minted ingest user {}", email)
        credential_by_sender[sender_class] = SenderCredential(
            sender_email=email,
            authorization_header_value=SecretStr(build_basic_authorization_header(email, password)),
            is_newly_minted=True,
        )
    return credential_by_sender


def apply_log_stream_retention(api: OpenObserveApiInterface, retention_days: int) -> dict[str, bool]:
    """Override every known log stream's retention; False marks streams that do not exist yet.

    Streams are created by OpenObserve on first ingest, so early provisioning
    passes legitimately find none -- re-run the accounts pass
    (``just provision-observability-accounts``) after data flows.
    """
    is_applied_by_stream: dict[str, bool] = {}
    for stream_name in ALL_LOG_STREAM_NAMES:
        is_applied = api.update_stream_retention(stream_name, "logs", retention_days)
        if not is_applied:
            logger.info("Skipped retention on stream {} (not created yet; re-run after data flows)", stream_name)
        is_applied_by_stream[stream_name] = is_applied
    return is_applied_by_stream

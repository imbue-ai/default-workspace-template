import httpx

from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.share_materials_injection import render_grants_toml
from imbue.minds.desktop_client.sharing_handler import _parse_grants_toml
from imbue.minds.desktop_client.sharing_handler import describe_connector_failure
from imbue.minds.desktop_client.sharing_handler import probe_share_readiness

_DOMAIN = "host-" + "a" * 32 + "." + "b" * 32 + ".us1.shares.example"


def test_probe_share_readiness_true_on_any_http_response() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == f"https://{_DOMAIN}/"
        return httpx.Response(302, headers={"location": "https://accounts.example/share/authorize?x=1"})

    client = httpx.Client(transport=httpx.MockTransport(_handler), follow_redirects=False)
    assert probe_share_readiness(client, _DOMAIN) is True


def test_probe_share_readiness_true_even_on_403() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    client = httpx.Client(transport=httpx.MockTransport(_handler), follow_redirects=False)
    assert probe_share_readiness(client, _DOMAIN) is True


def test_probe_share_readiness_false_on_transport_error() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("relay not reachable")

    client = httpx.Client(transport=httpx.MockTransport(_handler), follow_redirects=False)
    assert probe_share_readiness(client, _DOMAIN) is False


def test_grants_toml_roundtrips_through_render_and_parse() -> None:
    workspace_grants = {"emails": ["bob@example.com"], "email_domains": ["partner.org"]}
    service_grants = {
        "web": {"emails": ["carol@example.com"], "email_domains": []},
        "my-app": {"emails": [], "email_domains": ["viewer.dev"]},
    }

    rendered = render_grants_toml(workspace_grants, service_grants)
    parsed_workspace, parsed_services = _parse_grants_toml(rendered)

    assert parsed_workspace == workspace_grants
    assert parsed_services == service_grants


def test_parse_grants_toml_tolerates_malformation_as_empty() -> None:
    workspace_grants, service_grants = _parse_grants_toml("not toml [[")
    assert workspace_grants == {"emails": [], "email_domains": []}
    assert service_grants == {}

    workspace_grants, service_grants = _parse_grants_toml("workspace = 'not-a-table'")
    assert workspace_grants == {"emails": [], "email_domains": []}
    assert service_grants == {}


def test_describe_connector_failure_reports_an_expired_session() -> None:
    # Not "signed out": the account is still in this device's credential list,
    # so the app goes on showing it as signed in.
    exc = ImbueCloudCliError("shares create failed: Refresh rejected by connector: Session missing in db")
    message = describe_connector_failure(exc)
    assert message == "Your Imbue Cloud session has expired. You may need to log out and log in again."
    assert "signed out" not in message


def test_describe_connector_failure_reports_an_unverified_email() -> None:
    exc = ImbueCloudCliError('sync records push failed: Unauthenticated (401): {"detail":"Email not verified"}')
    assert describe_connector_failure(exc) == (
        "Imbue Cloud has not verified this account's email address. Verify it, then retry."
    )


def test_describe_connector_failure_keeps_an_unrecognized_message() -> None:
    # Better the connector's own wording than a pointer to a log file.
    exc = ImbueCloudCliError("shares create failed: Connector error 500: upstream exploded")
    assert describe_connector_failure(exc) == "shares create failed: Connector error 500: upstream exploded"

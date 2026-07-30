import base64
import tomllib

import pytest

from imbue.minds.desktop_client.share_materials_injection import ShareInjectionError
from imbue.minds.desktop_client.share_materials_injection import build_share_env_text
from imbue.minds.desktop_client.share_materials_injection import clear_share_materials_from_agent
from imbue.minds.desktop_client.share_materials_injection import inject_share_grants_into_agent
from imbue.minds.desktop_client.share_materials_injection import inject_share_materials_into_agent
from imbue.minds.desktop_client.share_materials_injection import render_grants_toml
from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.mngr.primitives import AgentId

_DOMAIN = "host-" + "a" * 32 + "." + "b" * 32 + ".us1.shares.example"


def test_build_share_env_text_matches_the_gateway_contract() -> None:
    text = build_share_env_text(
        workspace_domain=_DOMAIN,
        relay_endpoint="relay-us1.infra.example:7000",
        relay_token="tok-123",
        connector_url="https://connector.example",
        broker_url="https://accounts.example",
    )
    assert f"export SHARE_WORKSPACE_DOMAIN={_DOMAIN}\n" in text
    assert "export SHARE_RELAY_ENDPOINT=relay-us1.infra.example:7000\n" in text
    assert "export SHARE_RELAY_TOKEN=tok-123\n" in text
    assert "export SHARE_CONNECTOR_URL=https://connector.example\n" in text
    assert "export SHARE_BROKER_URL=https://accounts.example\n" in text


def test_render_grants_toml_emits_valid_toml_with_quoted_entries() -> None:
    rendered = render_grants_toml(
        {"emails": ['weird"quote@example.com'], "email_domains": ["partner.org"]},
        {"my-app": {"emails": ["carol@example.com"], "email_domains": []}},
    )
    parsed = tomllib.loads(rendered)
    assert parsed["workspace"]["emails"] == ['weird"quote@example.com']
    assert parsed["workspace"]["email_domains"] == ["partner.org"]
    assert parsed["services"]["my-app"]["emails"] == ["carol@example.com"]


def test_inject_writes_files_via_base64_exec() -> None:
    caller = RecordingMngrCaller()
    agent_id = AgentId()

    inject_share_grants_into_agent(agent_id, "[workspace]\nemails = []\n", caller)
    inject_share_materials_into_agent(agent_id, "export SHARE_WORKSPACE_DOMAIN=x\n", caller)

    grants_call = " ".join(caller.calls[0])
    materials_call = " ".join(caller.calls[1])
    assert "share_grants.toml" in grants_call
    assert "share.env" in materials_call
    # The content rides base64-encoded so emails and tokens never need shell quoting.
    encoded = base64.b64encode(b"[workspace]\nemails = []\n").decode("ascii")
    assert encoded in grants_call


def test_inject_raises_on_exec_failure() -> None:
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=1, stderr="boom"))

    with pytest.raises(ShareInjectionError):
        inject_share_grants_into_agent(AgentId(), "[workspace]\n", caller)


def test_clear_share_materials_is_best_effort_and_no_start() -> None:
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=1, stderr="offline"))

    clear_share_materials_from_agent(AgentId(), caller)

    joined = " ".join(caller.calls[0])
    assert "rm -f" in joined
    assert "--no-start" in joined

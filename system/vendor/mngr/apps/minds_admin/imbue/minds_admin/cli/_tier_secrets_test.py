import click
import pytest

from imbue.minds_admin.cli._tier_secrets import boxes_collector_install_config_from_secret
from imbue.minds_admin.cli._tier_secrets import observability_tier_for_env_name
from imbue.minds_admin.cli._tier_secrets import ovh_config_from_vault_secret
from imbue.minds_admin.cli._tier_secrets import resolve_ovh_config
from imbue.observability.primitives import CollectorRole
from imbue.observability.primitives import ObservabilityTierName

_ALL_OVH_CREDENTIAL_ENV_VARS = (
    "OVH_APPLICATION_KEY",
    "OVH_APP_KEY",
    "OVH_APPLICATION_SECRET",
    "OVH_APP_SECRET",
    "OVH_CONSUMER_KEY",
    "OVH_CLIENT_ID",
    "OVH_CLIENT_SECRET",
)


def _clear_ovh_and_activation_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for env_var in _ALL_OVH_CREDENTIAL_ENV_VARS:
        monkeypatch.delenv(env_var, raising=False)
    monkeypatch.delenv("MINDS_ROOT_NAME", raising=False)


def test_observability_tier_maps_shared_tiers_to_themselves_and_everything_else_to_dev() -> None:
    # Production and staging each have their own observability instance; every
    # dev-* and ci-* env reports to the single shared dev instance.
    assert observability_tier_for_env_name("production") == "production"
    assert observability_tier_for_env_name("staging") == "staging"
    assert observability_tier_for_env_name("dev-josh") == "dev"
    assert observability_tier_for_env_name("ci-a1b2c3") == "dev"


def test_boxes_collector_config_is_none_while_the_credential_is_absent_or_empty() -> None:
    # An observability entry with no boxes credential yet (first bring-up) must be
    # a clean skip, not a failure -- exactly the old collector-env exit-3 semantics.
    tier = ObservabilityTierName("dev")
    assert boxes_collector_install_config_from_secret({}, tier, "minds-dev.com") is None
    assert boxes_collector_install_config_from_secret({"INGEST_CREDENTIAL_BOXES": ""}, tier, "minds-dev.com") is None


def test_boxes_collector_config_carries_the_tier_ingest_url_and_credential() -> None:
    secret = {"INGEST_CREDENTIAL_BOXES": "Basic Ym94ZXM6aHVudGVyMg==", "INGEST_CREDENTIAL_MODAL": "Basic other"}
    config = boxes_collector_install_config_from_secret(secret, ObservabilityTierName("staging"), "minds-staging.com")
    assert config is not None
    assert config.role == CollectorRole.BOX
    assert str(config.tier) == "staging"
    assert str(config.ingest_url) == "https://telemetry.minds-staging.com/"
    assert config.ingest_authorization_header_value.get_secret_value() == "Basic Ym94ZXM6aHVudGVyMg=="


def test_ovh_config_from_vault_secret_builds_explicit_credentials() -> None:
    secret = {
        "OVH_APPLICATION_KEY": "ak-36284",
        "OVH_APPLICATION_SECRET": "as-36284",
        "OVH_CONSUMER_KEY": "ck-36284",
        # Relay-only field in the same entry; must be ignored, not required.
        "OVH_CLOUD_PROJECT_ID": "project-36284",
    }
    config = ovh_config_from_vault_secret(secret, "secrets/minds/production")
    assert config.has_explicit_credentials()
    kwargs = config.resolve_python_ovh_kwargs()
    assert kwargs["application_key"] == "ak-36284"
    assert kwargs["application_secret"] == "as-36284"
    assert kwargs["consumer_key"] == "ck-36284"


def test_ovh_config_from_vault_secret_names_every_missing_field() -> None:
    with pytest.raises(click.ClickException) as exc_info:
        ovh_config_from_vault_secret({"OVH_APPLICATION_KEY": "ak-36284"}, "secrets/minds/staging")
    message = str(exc_info.value)
    assert "secrets/minds/staging/ovh" in message
    assert "OVH_APPLICATION_SECRET" in message
    assert "OVH_CONSUMER_KEY" in message
    assert "OVH_APPLICATION_KEY" not in message.split("missing")[1].split(";")[0]


def test_resolve_ovh_config_prefers_the_env_var_override(monkeypatch: pytest.MonkeyPatch) -> None:
    # The OVH_* env vars are the non-activated one-off escape hatch and win over
    # any activated tier's Vault entry (no Vault read happens at all: an activated
    # env in this test would fail the read, and no such failure surfaces).
    _clear_ovh_and_activation_env(monkeypatch)
    monkeypatch.setenv("OVH_APPLICATION_KEY", "env-ak-36284")
    monkeypatch.setenv("OVH_APPLICATION_SECRET", "env-as-36284")
    monkeypatch.setenv("OVH_CONSUMER_KEY", "env-ck-36284")
    monkeypatch.setenv("MINDS_ROOT_NAME", "minds-dev-testuser")
    config = resolve_ovh_config()
    kwargs = config.resolve_python_ovh_kwargs()
    assert kwargs["application_key"] == "env-ak-36284"
    assert kwargs["consumer_key"] == "env-ck-36284"


def test_resolve_ovh_config_without_env_vars_or_activation_gives_an_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_ovh_and_activation_env(monkeypatch)
    with pytest.raises(click.ClickException) as exc_info:
        resolve_ovh_config()
    message = str(exc_info.value)
    assert "minds-admin env activate" in message
    assert "OVH_APPLICATION_KEY" in message

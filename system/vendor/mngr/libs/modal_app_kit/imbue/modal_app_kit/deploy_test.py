import pytest

from imbue.modal_app_kit.deploy import DEPLOY_ENV_VAR
from imbue.modal_app_kit.deploy import DEPLOY_ID_ENV_VAR
from imbue.modal_app_kit.deploy import DEPLOY_ID_UNSET_SENTINEL
from imbue.modal_app_kit.deploy import deploy_metadata_entries
from imbue.modal_app_kit.deploy import forwarded_env_secret
from imbue.modal_app_kit.deploy import read_custom_domains
from imbue.modal_app_kit.deploy import read_deploy_env
from imbue.modal_app_kit.deploy import read_deploy_id
from imbue.modal_app_kit.deploy import read_min_containers
from imbue.modal_app_kit.deploy import read_modal_proxy
from imbue.modal_app_kit.deploy import read_scaledown_window
from imbue.modal_app_kit.deploy import stamped_secret_name


def test_read_deploy_env_defaults_to_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DEPLOY_ENV_VAR, raising=False)

    assert read_deploy_env() == "production"


def test_read_deploy_env_reads_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DEPLOY_ENV_VAR, "staging")

    assert read_deploy_env() == "staging"


def test_read_deploy_id_defaults_to_unset_sentinel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DEPLOY_ID_ENV_VAR, raising=False)

    assert read_deploy_id() == DEPLOY_ID_UNSET_SENTINEL


def test_read_deploy_id_reads_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DEPLOY_ID_ENV_VAR, "20260801t000000z")

    assert read_deploy_id() == "20260801t000000z"


def test_read_min_containers_defaults_to_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODAL_APP_KIT_TEST_MIN_CONTAINERS_73519", raising=False)

    assert read_min_containers("MODAL_APP_KIT_TEST_MIN_CONTAINERS_73519") == 0


def test_read_min_containers_reads_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODAL_APP_KIT_TEST_MIN_CONTAINERS_73519", "2")

    assert read_min_containers("MODAL_APP_KIT_TEST_MIN_CONTAINERS_73519") == 2


def test_read_scaledown_window_normalizes_zero_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODAL_APP_KIT_TEST_SCALEDOWN_73519", "0")

    assert read_scaledown_window("MODAL_APP_KIT_TEST_SCALEDOWN_73519") is None


def test_read_scaledown_window_reads_positive_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODAL_APP_KIT_TEST_SCALEDOWN_73519", "600")

    assert read_scaledown_window("MODAL_APP_KIT_TEST_SCALEDOWN_73519") == 600


def test_stamped_secret_name_joins_service_tier_and_deploy_id() -> None:
    assert stamped_secret_name("cloudflare", "staging", "20260801t000000z") == "cloudflare-staging-20260801t000000z"


def test_read_custom_domains_returns_none_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODAL_APP_KIT_TEST_CUSTOM_DOMAINS_73519", raising=False)

    assert read_custom_domains("MODAL_APP_KIT_TEST_CUSTOM_DOMAINS_73519") is None


def test_read_custom_domains_splits_comma_separated_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODAL_APP_KIT_TEST_CUSTOM_DOMAINS_73519", "accounts.example.com, minds.example.com")

    assert read_custom_domains("MODAL_APP_KIT_TEST_CUSTOM_DOMAINS_73519") == [
        "accounts.example.com",
        "minds.example.com",
    ]


def test_read_custom_domains_returns_none_for_an_empty_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODAL_APP_KIT_TEST_CUSTOM_DOMAINS_73519", " , ")

    assert read_custom_domains("MODAL_APP_KIT_TEST_CUSTOM_DOMAINS_73519") is None


def test_deploy_metadata_entries_carry_tier_and_deploy_id_only_by_default() -> None:
    assert deploy_metadata_entries("staging", "20260801t000000z", {}) == {
        DEPLOY_ENV_VAR: "staging",
        DEPLOY_ID_ENV_VAR: "20260801t000000z",
    }


def test_deploy_metadata_entries_thread_the_log_level_knob_when_the_deployer_exported_it() -> None:
    entries = deploy_metadata_entries("dev", "20260801t000000z", {"MINDS_LOG_LEVEL": "DEBUG", "MINDS_OTHER": "x"})

    assert entries["MINDS_LOG_LEVEL"] == "DEBUG"
    assert "MINDS_OTHER" not in entries


def test_deploy_metadata_entries_drop_an_empty_log_level_knob() -> None:
    assert "MINDS_LOG_LEVEL" not in deploy_metadata_entries("dev", "id", {"MINDS_LOG_LEVEL": ""})


def test_forwarded_env_secret_carries_every_named_var_even_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODAL_APP_KIT_TEST_FWD_SET_61832", "value")
    monkeypatch.delenv("MODAL_APP_KIT_TEST_FWD_UNSET_61832", raising=False)

    secret = forwarded_env_secret(("MODAL_APP_KIT_TEST_FWD_SET_61832", "MODAL_APP_KIT_TEST_FWD_UNSET_61832"))

    # The lazy Secret's repr lists its keys; an unset var must still be
    # present (as an empty string) so the container env always defines it.
    assert "MODAL_APP_KIT_TEST_FWD_SET_61832" in repr(secret)
    assert "MODAL_APP_KIT_TEST_FWD_UNSET_61832" in repr(secret)


def test_read_modal_proxy_returns_none_when_unset_or_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODAL_APP_KIT_TEST_MODAL_PROXY_84217", raising=False)
    monkeypatch.delenv("MODAL_APP_KIT_TEST_MODAL_PROXY_ENV_84217", raising=False)
    assert read_modal_proxy("MODAL_APP_KIT_TEST_MODAL_PROXY_84217", "MODAL_APP_KIT_TEST_MODAL_PROXY_ENV_84217") is None

    monkeypatch.setenv("MODAL_APP_KIT_TEST_MODAL_PROXY_84217", "   ")
    assert read_modal_proxy("MODAL_APP_KIT_TEST_MODAL_PROXY_84217", "MODAL_APP_KIT_TEST_MODAL_PROXY_ENV_84217") is None


def test_read_modal_proxy_builds_a_lazy_proxy_from_the_named_value(monkeypatch: pytest.MonkeyPatch) -> None:
    # Proxy.from_name is lazy (hydrated at deploy), so a fake name is safe here.
    monkeypatch.setenv("MODAL_APP_KIT_TEST_MODAL_PROXY_84217", "unit-test-proxy")
    monkeypatch.delenv("MODAL_APP_KIT_TEST_MODAL_PROXY_ENV_84217", raising=False)

    proxy = read_modal_proxy("MODAL_APP_KIT_TEST_MODAL_PROXY_84217", "MODAL_APP_KIT_TEST_MODAL_PROXY_ENV_84217")

    assert proxy is not None


def test_read_modal_proxy_resolves_in_the_named_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # Proxy lookup is environment-scoped: a shared proxy lives in one
    # environment (e.g. 'main') while apps deploy into per-env environments,
    # so the threaded environment name must reach Proxy.from_name.
    monkeypatch.setenv("MODAL_APP_KIT_TEST_MODAL_PROXY_84217", "unit-test-proxy")
    monkeypatch.setenv("MODAL_APP_KIT_TEST_MODAL_PROXY_ENV_84217", "main")

    proxy = read_modal_proxy("MODAL_APP_KIT_TEST_MODAL_PROXY_84217", "MODAL_APP_KIT_TEST_MODAL_PROXY_ENV_84217")

    assert proxy is not None
    # The lazy Proxy's repr names the resolution environment; hydration (and so
    # any richer inspection) needs a live Modal client.
    assert "environment_name='main'" in repr(proxy)

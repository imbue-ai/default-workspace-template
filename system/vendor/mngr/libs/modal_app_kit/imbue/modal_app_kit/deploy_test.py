import pytest

from imbue.modal_app_kit.deploy import DEPLOY_ENV_VAR
from imbue.modal_app_kit.deploy import DEPLOY_ID_ENV_VAR
from imbue.modal_app_kit.deploy import DEPLOY_ID_UNSET_SENTINEL
from imbue.modal_app_kit.deploy import deploy_metadata_entries
from imbue.modal_app_kit.deploy import read_custom_domains
from imbue.modal_app_kit.deploy import read_deploy_env
from imbue.modal_app_kit.deploy import read_deploy_id
from imbue.modal_app_kit.deploy import read_min_containers
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

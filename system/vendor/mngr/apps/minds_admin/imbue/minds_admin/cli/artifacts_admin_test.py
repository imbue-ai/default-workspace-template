import pytest
from click.testing import CliRunner

from imbue.minds_admin.cli.root import cli
from imbue.minds_admin.slices.mirror_artifacts import MIRROR_ARTIFACTS


def test_artifacts_list_prints_every_manifest_entry_with_its_upstream_and_digest() -> None:
    result = CliRunner().invoke(cli, ["artifacts", "list"])
    assert result.exit_code == 0, result.output
    for artifact in MIRROR_ARTIFACTS:
        assert artifact.mirror_url in result.output
        assert artifact.upstream_url in result.output
        assert artifact.digest in result.output


def test_artifacts_upload_refuses_an_unknown_name_before_touching_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APT_MIRROR_R2_ENDPOINT", raising=False)
    result = CliRunner().invoke(cli, ["artifacts", "upload", "--name", "no-such-artifact"])
    assert result.exit_code == 2
    assert "unknown artifact name" in result.output


def test_artifacts_verify_without_r2_credentials_reports_a_clean_error(monkeypatch: pytest.MonkeyPatch) -> None:
    for env_var in (
        "APT_MIRROR_R2_ENDPOINT",
        "APT_MIRROR_R2_BUCKET",
        "APT_MIRROR_R2_ACCESS_KEY_ID",
        "APT_MIRROR_R2_SECRET_ACCESS_KEY",
    ):
        monkeypatch.delenv(env_var, raising=False)
    result = CliRunner().invoke(cli, ["artifacts", "verify"])
    assert result.exit_code == 2
    assert "APT_MIRROR_R2_ENDPOINT" in result.output

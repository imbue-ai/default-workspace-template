from pathlib import Path

from owner_exec.main import read_share_audience


def test_read_share_audience_extracts_workspace_domain(tmp_path: Path) -> None:
    share_env = tmp_path / "share.env"
    share_env.write_text(
        "export SHARE_WORKSPACE_DOMAIN=Host-Abc.user.us1.imbueminds.com\n"
        "export SHARE_RELAY_TOKEN=tok\n"
    )
    assert read_share_audience(share_env) == "host-abc.user.us1.imbueminds.com"


def test_read_share_audience_is_empty_when_file_absent(tmp_path: Path) -> None:
    assert read_share_audience(tmp_path / "absent.env") == ""


def test_read_share_audience_is_empty_when_key_missing(tmp_path: Path) -> None:
    share_env = tmp_path / "share.env"
    share_env.write_text("export SHARE_RELAY_TOKEN=tok\n")
    assert read_share_audience(share_env) == ""

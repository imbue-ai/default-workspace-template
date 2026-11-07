import base64
import json
import os
import stat
from pathlib import Path

import pytest

from auth_sandbox.errors import MissingMaterialError, SessionFileError
from auth_sandbox.runtime_files import SandboxPaths
from auth_sandbox.session import (
    SessionSettings,
    install_session,
    parse_session_file,
    read_and_remove_session_file,
    read_session_settings,
)

_KEY = base64.b64encode(bytes(range(32))).decode("ascii")
_STATE = b"\x00encrypted browser state"


def _session_text(**overrides: object) -> str:
    document: dict[str, object] = {
        "latchkey_key": _KEY,
        "browser_state": base64.b64encode(_STATE).decode("ascii"),
        "workspace_host_id": "host-" + "a" * 32,
        "timezone": "America/Argentina/Buenos_Aires",
    }
    document.update(overrides)
    return json.dumps(document)


def test_parse_session_file_reads_every_field() -> None:
    material = parse_session_file(_session_text())
    assert material.latchkey_key.get_secret_value() == _KEY
    assert material.browser_state is not None
    assert material.browser_state.get_secret_value() == _STATE
    assert material.settings == SessionSettings(
        workspace_host_id="host-" + "a" * 32, timezone="America/Argentina/Buenos_Aires"
    )


def test_parse_session_file_takes_null_or_absent_optional_fields_and_ignores_unknown_ones() -> (
    None
):
    material = parse_session_file(
        json.dumps(
            {"latchkey_key": _KEY, "browser_state": None, "timezone": "", "later": 1}
        )
    )
    assert material.browser_state is None
    assert material.settings == SessionSettings(workspace_host_id=None, timezone=None)


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("{", "not valid JSON"),
        ("[]", "must hold a JSON object"),
        (_session_text(latchkey_key=None), "'latchkey_key' must be a non-empty string"),
        (_session_text(latchkey_key="not base64!"), "'latchkey_key' is not base64"),
        (
            _session_text(latchkey_key=base64.b64encode(b"short").decode()),
            "must be 32 bytes",
        ),
        (_session_text(browser_state="%%%"), "'browser_state' is not base64"),
        (_session_text(browser_state=7), "'browser_state' must be a string or null"),
        (
            _session_text(workspace_host_id=["host-a"]),
            "'workspace_host_id' must be a string or null",
        ),
        (
            _session_text(timezone="Europe/Paris\nTILION_SEED=1"),
            "not an IANA zone name",
        ),
        (_session_text(timezone="../etc"), "not an IANA zone name"),
    ],
)
def test_parse_session_file_refuses_a_malformed_session_without_quoting_the_key(
    text: str, reason: str
) -> None:
    with pytest.raises(SessionFileError, match=reason) as refused:
        parse_session_file(text)
    assert _KEY not in str(refused.value)


def test_the_session_file_is_deleted_once_read_whether_or_not_it_parses(
    tmp_path: Path,
) -> None:
    good = tmp_path / "session.json"
    good.write_text(_session_text())
    assert read_and_remove_session_file(good).latchkey_key.get_secret_value() == _KEY
    assert not good.exists()

    bad = tmp_path / "bad.json"
    bad.write_text(_session_text(latchkey_key="x"))
    with pytest.raises(SessionFileError):
        read_and_remove_session_file(bad)
    assert not bad.exists()

    with pytest.raises(MissingMaterialError, match="no session file"):
        read_and_remove_session_file(tmp_path / "absent.json")


def test_install_session_writes_the_state_settings_and_key_privately(
    tmp_path: Path,
) -> None:
    paths = SandboxPaths(runtime_dir=tmp_path / "run")
    paths.runtime_dir.mkdir()

    install_session(parse_session_file(_session_text()), paths)

    assert paths.latchkey_key_path.read_text() == _KEY + "\n"
    assert paths.latchkey_browser_state_path.read_bytes() == _STATE
    assert read_session_settings(paths.session_settings_path) == SessionSettings(
        workspace_host_id="host-" + "a" * 32, timezone="America/Argentina/Buenos_Aires"
    )
    for written in (
        paths.latchkey_key_path,
        paths.latchkey_browser_state_path,
        paths.session_settings_path,
    ):
        assert stat.S_IMODE(os.stat(written).st_mode) == 0o600


def test_install_session_without_a_browser_state_writes_none(tmp_path: Path) -> None:
    paths = SandboxPaths(runtime_dir=tmp_path / "run")
    paths.runtime_dir.mkdir()
    install_session(parse_session_file(_session_text(browser_state=None)), paths)
    assert not paths.latchkey_browser_state_path.exists()
    assert paths.latchkey_key_path.is_file()


def test_read_session_settings_needs_prepare_to_have_run_and_its_own_shape(
    tmp_path: Path,
) -> None:
    with pytest.raises(MissingMaterialError, match="no session settings"):
        read_session_settings(tmp_path / "session_settings.json")
    (tmp_path / "session_settings.json").write_text('{"timezone": 3}')
    with pytest.raises(SessionFileError, match="malformed"):
        read_session_settings(tmp_path / "session_settings.json")

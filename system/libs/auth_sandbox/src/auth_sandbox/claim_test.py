import json
import os
import stat
from pathlib import Path

import pytest
from click.testing import CliRunner
from loguru import logger

from auth_sandbox.claim import (
    Claim,
    ClaimPaths,
    parse_claim,
    serve_claim,
)
from auth_sandbox.cli import await_claim_command
from auth_sandbox.errors import ClaimFileError
from auth_sandbox.mock_latchkey_test import (
    FakeClock,
    ScheduledWrite,
)

_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOwner owner@example.com"
_DIGEST = "0123456789abcdef" * 4
_START = 1_000_000.0


def _claim_text(**overrides: object) -> str:
    document: dict[str, object] = {
        "authorized_key": _KEY,
        "screen_token_sha256": _DIGEST,
        "expires_at": int(_START + 600),
    }
    document.update(overrides)
    return json.dumps(document)


def _paths(tmp_path: Path) -> ClaimPaths:
    (tmp_path / "ssh").mkdir()
    (tmp_path / "ssh" / "authorized_keys").write_text("")
    (tmp_path / "run").mkdir()
    return ClaimPaths(
        claim_path=tmp_path / "auth-sandbox-claim.json",
        authorized_keys_path=tmp_path / "ssh" / "authorized_keys",
        screen_token_sha256_path=tmp_path / "run" / "screen_token_sha256",
    )


def _errors_logged() -> tuple[list[str], int]:
    errors: list[str] = []
    return errors, logger.add(
        lambda message: errors.append(message.record["message"]), level="ERROR"
    )


def test_parse_claim_reads_the_three_fields() -> None:
    assert parse_claim(_claim_text()) == Claim(
        authorized_key=_KEY, screen_token_sha256=_DIGEST, expires_at=_START + 600
    )
    assert parse_claim(_claim_text(expires_at=1.5)).expires_at == 1.5


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("{", "not valid JSON"),
        ("[]", "must hold a JSON object"),
        (_claim_text(authorized_key=None), "authorized_key must be a non-empty string"),
        (_claim_text(authorized_key="  "), "authorized_key must be a non-empty string"),
        (_claim_text(authorized_key=_KEY + "\nssh-ed25519 AAAA two"), "one line"),
        (_claim_text(authorized_key=_KEY + "\r"), "one line"),
        (_claim_text(screen_token_sha256=_DIGEST.upper()), "64 lower-case hex digits"),
        (_claim_text(screen_token_sha256=_DIGEST[:-1]), "64 lower-case hex digits"),
        (_claim_text(screen_token_sha256=None), "64 lower-case hex digits"),
        (_claim_text(expires_at=True), "positive unix time"),
        (_claim_text(expires_at="1700000000"), "positive unix time"),
        (_claim_text(expires_at=0), "positive unix time"),
        (_claim_text(expires_at=None), "positive unix time"),
        (_claim_text().replace(str(int(_START + 600)), "NaN"), "positive unix time"),
    ],
)
def test_parse_claim_refuses_a_malformed_claim(text: str, reason: str) -> None:
    with pytest.raises(ClaimFileError, match=reason):
        parse_claim(text)


def test_serve_claim_waits_for_the_claim_installs_it_privately_and_returns_at_its_expiry(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    clock = FakeClock(
        current=_START,
        scheduled=[
            ScheduledWrite(at=_START + 30, path=paths.claim_path, text=_claim_text())
        ],
    )

    serve_claim(paths, clock, 0.25)

    assert paths.authorized_keys_path.read_text() == _KEY + "\n"
    assert paths.screen_token_sha256_path.read_text() == _DIGEST + "\n"
    for installed in (paths.authorized_keys_path, paths.screen_token_sha256_path):
        assert stat.S_IMODE(os.stat(installed).st_mode) == 0o600
    # Polled every quarter second until the claim, then one wait to the expiry and no further.
    assert clock.pauses[:120] == [0.25] * 120
    assert clock.pauses[120:] == [570.0]
    assert clock.current == _START + 600
    assert sorted(
        path.name for path in paths.authorized_keys_path.parent.iterdir()
    ) == ["authorized_keys"]


def test_a_refused_claim_is_logged_once_and_a_rewritten_valid_one_is_installed(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    paths.claim_path.write_text(_claim_text(screen_token_sha256="not-a-digest"))
    # One watcher sees the refused file, polls it unchanged, then sees the connector's rewrite.
    clock = FakeClock(
        current=_START,
        scheduled=[
            ScheduledWrite(at=_START + 30, path=paths.claim_path, text=_claim_text())
        ],
    )
    errors, handle = _errors_logged()
    try:
        serve_claim(paths, clock, 0.25)
    finally:
        logger.remove(handle)
    assert len(errors) == 1
    assert "64 lower-case hex digits" in errors[0]
    assert paths.authorized_keys_path.read_text() == _KEY + "\n"
    assert paths.screen_token_sha256_path.read_text() == _DIGEST + "\n"
    assert clock.pauses[:120] == [0.25] * 120
    assert clock.pauses[120:] == [570.0]


def test_a_claim_that_arrives_already_expired_admits_no_one(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.claim_path.write_text(_claim_text(expires_at=int(_START - 1)))
    clock = FakeClock(current=_START)
    errors, handle = _errors_logged()
    try:
        serve_claim(paths, clock, 0.25)
    finally:
        logger.remove(handle)
    assert paths.authorized_keys_path.read_text() == ""
    assert not paths.screen_token_sha256_path.exists()
    assert clock.pauses == []
    assert len(errors) == 1
    assert "before it arrived" in errors[0]


def test_the_command_ends_on_an_expired_claim_and_exits_3_when_it_cannot_install(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    paths.claim_path.write_text(_claim_text(expires_at=1))
    arguments = [
        "--claim-file",
        str(paths.claim_path),
        "--authorized-keys",
        str(paths.authorized_keys_path),
        "--runtime-dir",
        str(paths.screen_token_sha256_path.parent),
    ]
    expired = CliRunner().invoke(await_claim_command, arguments)
    assert expired.exit_code == 0, expired.output
    assert paths.authorized_keys_path.read_text() == ""

    paths.claim_path.write_text(_claim_text(expires_at=4_000_000_000))
    unwritable = CliRunner().invoke(
        await_claim_command,
        [*arguments[:-1], str(tmp_path / "no-such-runtime-dir")],
    )
    assert unwritable.exit_code == 3
    assert "no-such-runtime-dir" in unwritable.output

import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from auth_sandbox.errors import LatchkeyCommandError
from auth_sandbox.interfaces import FinishedCommand
from auth_sandbox.mock_latchkey_test import FakeLatchkeyRunner
from auth_sandbox.store_queries import (
    describe_service,
    store_environment,
    stored_accounts,
)

_BASE_ENV = {
    "PATH": "/usr/bin",
    "LATCHKEY_GATEWAY": "http://host.docker.internal:1989",
    "HOME": "/home/user",
}
_SLACK_INFO = {"type": "built-in", "authOptions": ["browser", "set"], "credentials": {}}
_LISTING = {
    "slack": {"": {"credentialType": "rawCurl", "credentialStatus": "unknown"}},
    "github": {
        "me@example.com": {"credentialType": "oauth", "credentialStatus": "unknown"}
    },
}


def _answering(
    stdout: str, returncode: int = 0, stderr: str = ""
) -> FakeLatchkeyRunner:
    return FakeLatchkeyRunner(
        answer=FinishedCommand(returncode=returncode, stdout=stdout, stderr=stderr)
    )


def test_store_environment_names_the_store_and_key_and_drops_the_gateway() -> None:
    assert store_environment(
        _BASE_ENV, Path("/run/auth-sandbox/latchkey"), SecretStr("k3y")
    ) == {
        "PATH": "/usr/bin",
        "HOME": "/home/user",
        "LATCHKEY_DIRECTORY": "/run/auth-sandbox/latchkey",
        "LATCHKEY_ENCRYPTION_KEY": "k3y",
        "LATCHKEY_DISABLE_COUNTING": "1",
    }


def test_describe_service_answers_latchkeys_offline_description() -> None:
    runner = _answering(json.dumps(_SLACK_INFO, indent=2))
    assert describe_service(runner, "/opt/lk", {"K": "v"}, "slack") == _SLACK_INFO
    (run,) = runner.runs
    assert run.argv == ("/opt/lk", "services", "info", "slack", "--offline")
    assert run.environment == {"K": "v"}


def test_describe_service_answers_nothing_for_a_service_latchkey_does_not_know() -> (
    None
):
    runner = _answering(
        "",
        returncode=1,
        stderr="Error: Unknown service: gitlabx. Use 'latchkey services list' to see available services.",
    )
    assert describe_service(runner, "latchkey", {}, "gitlabx") == {}


@pytest.mark.parametrize(
    ("runner", "reason"),
    [
        (
            _answering("", returncode=1, stderr="Error: config.json is not valid JSON"),
            "config.json is not valid",
        ),
        (_answering("not json"), "printed no JSON"),
        (_answering("[1]"), "not an object"),
    ],
)
def test_describe_service_reports_any_other_failure(
    runner: FakeLatchkeyRunner, reason: str
) -> None:
    with pytest.raises(LatchkeyCommandError, match=reason):
        describe_service(runner, "latchkey", {}, "slack")


def test_stored_accounts_narrows_the_listing_to_the_service() -> None:
    runner = _answering(json.dumps(_LISTING))
    assert stored_accounts(runner, "latchkey", {}, "github") == _LISTING["github"]
    assert runner.runs[0].argv == ("latchkey", "auth", "list", "--offline")
    assert stored_accounts(runner, "latchkey", {}, "linear") == {}


def test_stored_accounts_reports_a_failed_or_misshapen_listing() -> None:
    with pytest.raises(
        LatchkeyCommandError,
        match="listing the stored accounts failed: Error: Failed to decrypt",
    ):
        stored_accounts(
            _answering(
                "",
                returncode=1,
                stderr="Error: Failed to decrypt. The encryption key may have changed.",
            ),
            "latchkey",
            {},
            "slack",
        )
    with pytest.raises(LatchkeyCommandError, match="other than an object"):
        stored_accounts(_answering('{"slack": []}'), "latchkey", {}, "slack")

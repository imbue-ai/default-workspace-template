import json
from pathlib import Path

import pytest
from imbue.mngr_latchkey.core import CredentialStatus, LatchkeyServiceInfo

from auth_sandbox.errors import ValuesFileError
from auth_sandbox.mock_latchkey_test import FakeLatchkey
from auth_sandbox.set_credentials import (
    CredentialValues,
    parse_values_file,
    read_and_remove_values_file,
    run_set_credentials,
)

_AWS_EXAMPLE = "latchkey auth set-nocurl aws <access-key-id> <secret-access-key>"


def _aws_info(example: str | None) -> LatchkeyServiceInfo:
    return LatchkeyServiceInfo(
        credential_status=CredentialStatus.MISSING,
        accounts=(),
        auth_options=frozenset({"set"}),
        set_credentials_example=example,
    )


def test_parse_values_file_reads_values_and_the_optional_example() -> None:
    parsed = parse_values_file(
        json.dumps(
            {"values": {"token": "t0k"}, "set_credentials_example": _AWS_EXAMPLE}
        )
    )
    assert parsed == CredentialValues(
        value_by_parameter_name={"token": "t0k"}, set_credentials_example=_AWS_EXAMPLE
    )
    assert parse_values_file(json.dumps({"values": {}})).set_credentials_example is None


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        json.dumps({"values": ["a"]}),
        json.dumps({"values": {"a": 1}}),
        json.dumps({"values": {}, "set_credentials_example": 3}),
    ],
)
def test_parse_values_file_refuses_other_shapes_without_quoting_values(
    text: str,
) -> None:
    with pytest.raises(ValuesFileError) as excinfo:
        parse_values_file(text)
    assert "t0k" not in str(excinfo.value)


def test_read_and_remove_values_file_deletes_the_file_once_read(tmp_path: Path) -> None:
    values_path = tmp_path / "values.json"
    values_path.write_text(json.dumps({"values": {"token": "t0k"}}))
    assert read_and_remove_values_file(values_path).value_by_parameter_name == {
        "token": "t0k"
    }
    assert not values_path.exists()
    with pytest.raises(ValuesFileError, match="no values file"):
        read_and_remove_values_file(values_path)
    (tmp_path / "a-directory").mkdir()
    with pytest.raises(ValuesFileError, match="cannot be read"):
        read_and_remove_values_file(tmp_path / "a-directory")


def test_run_set_credentials_builds_the_desktops_argv_from_latchkeys_example() -> None:
    latchkey = FakeLatchkey(service_info=_aws_info(_AWS_EXAMPLE))
    values = CredentialValues(
        value_by_parameter_name={
            "access-key-id": "AKIA1",
            "secret-access-key": " s3cr3t ",
        },
        set_credentials_example=None,
    )

    outcome = run_set_credentials(latchkey, "aws", "work", values)

    assert outcome.is_stored is True
    assert latchkey.initialized_count == 1
    assert latchkey.info_calls == ["aws"]
    assert latchkey.set_calls == [
        ("aws", ("--account", "work", "auth", "set-nocurl", "aws", "AKIA1", "s3cr3t"))
    ]


def test_run_set_credentials_prefers_the_example_the_chrome_passed_and_falls_back_to_bearer() -> (
    None
):
    passed = FakeLatchkey(service_info=_aws_info("latchkey auth set aws <other>"))
    values = CredentialValues(
        value_by_parameter_name={"access-key-id": "A", "secret-access-key": "S"},
        set_credentials_example=_AWS_EXAMPLE,
    )
    run_set_credentials(passed, "aws", "work", values)
    assert passed.info_calls == []
    assert passed.set_calls[0][1][3:5] == ("set-nocurl", "aws")

    unknown = FakeLatchkey(service_info=None)
    run_set_credentials(
        unknown,
        "mystery",
        "work",
        CredentialValues(
            value_by_parameter_name={"token": "tk"}, set_credentials_example=None
        ),
    )
    assert unknown.set_calls == [
        (
            "mystery",
            (
                "--account",
                "work",
                "auth",
                "set",
                "mystery",
                "-H",
                "Authorization: Bearer tk",
            ),
        )
    ]


def test_run_set_credentials_reports_a_blank_value_without_running_latchkey() -> None:
    latchkey = FakeLatchkey(service_info=_aws_info(_AWS_EXAMPLE))
    values = CredentialValues(
        value_by_parameter_name={"access-key-id": "AKIA1"}, set_credentials_example=None
    )

    outcome = run_set_credentials(latchkey, "aws", "work", values)

    assert outcome.is_stored is False
    assert "Secret access key" in outcome.detail
    assert latchkey.set_calls == []


def test_run_set_credentials_passes_latchkeys_refusal_through() -> None:
    latchkey = FakeLatchkey(
        service_info=_aws_info(_AWS_EXAMPLE),
        is_set_stored=False,
        set_detail="doesn't look like an AWS key",
    )
    values = CredentialValues(
        value_by_parameter_name={"access-key-id": "x", "secret-access-key": "y"},
        set_credentials_example=None,
    )
    outcome = run_set_credentials(latchkey, "aws", "work", values)
    assert outcome == outcome.model_copy(
        update={"is_stored": False, "detail": "doesn't look like an AWS key"}
    )

from collections.abc import Mapping

import pytest

from scripts.r2.client import R2CredentialsError
from scripts.r2.client import has_r2_credentials
from scripts.r2.client import read_r2_credentials


def test_a_missing_r2_credential_is_named_rather_than_left_to_boto3() -> None:
    with pytest.raises(R2CredentialsError, match="R2_SECRET_ACCESS_KEY"):
        read_r2_credentials({"R2_ACCOUNT_ID": "acct", "R2_ACCESS_KEY_ID": "key"})


def test_an_empty_r2_credential_counts_as_missing() -> None:
    """A publish job exports all three names, so a secret Vault did not supply arrives empty.

    Left unchecked it reaches boto3 as `https://.r2.cloudflarestorage.com`, and
    the operator sees an endpoint error rather than the name of the secret.
    """
    with pytest.raises(R2CredentialsError, match="R2_ACCESS_KEY_ID"):
        read_r2_credentials({"R2_ACCOUNT_ID": "acct", "R2_ACCESS_KEY_ID": "", "R2_SECRET_ACCESS_KEY": "secret"})


def test_the_reachability_check_answers_for_exactly_the_environments_that_can_be_read() -> None:
    """`release_channel/publish.py` picks its reader with this predicate.

    True sends a promotion's rollback gate to the bucket, False to the CDN copy,
    which is served with a max-age and can answer with the previous promotion.
    So the two functions must agree on what counts as present -- including that
    empty counts as missing, since a publish workflow exports all three names
    unconditionally. If they disagreed, a run would announce the bucket and then
    be refused by the credential read it had just chosen.
    """
    complete = {"R2_ACCOUNT_ID": "acct", "R2_ACCESS_KEY_ID": "key", "R2_SECRET_ACCESS_KEY": "secret"}
    environments: tuple[Mapping[str, str], ...] = (
        complete,
        {},
        {name: "" for name in complete},
        {**complete, "R2_SECRET_ACCESS_KEY": ""},
    )
    for env in environments:
        assert has_r2_credentials(env) == _is_readable(env), env


def _is_readable(env: Mapping[str, str]) -> bool:
    try:
        read_r2_credentials(env)
    except R2CredentialsError:
        return False
    return True


def test_complete_r2_credentials_are_read() -> None:
    credentials = read_r2_credentials(
        {"R2_ACCOUNT_ID": "acct", "R2_ACCESS_KEY_ID": "key", "R2_SECRET_ACCESS_KEY": "secret"}
    )
    assert (credentials.account_id, credentials.access_key_id, credentials.secret_access_key) == (
        "acct",
        "key",
        "secret",
    )
    assert credentials.endpoint_url == "https://acct.r2.cloudflarestorage.com"

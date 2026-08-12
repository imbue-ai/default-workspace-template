"""Tests for the in-workspace backup provisioning script.

The pure parsing and the missing-key guard are exercised directly; the real
``restic init`` round trip runs only when a ``restic`` binary is available (a
local filesystem repository needs no network), and is skipped otherwise.
"""

import importlib.util
import shutil
import uuid
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).parent / "provision_backups.py"


def _load_provision_backups() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_provision_backups_under_test", _SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MODULE = _load_provision_backups()


def test_parse_restic_env_file_handles_export_quotes_and_comments() -> None:
    parsed = _MODULE.parse_restic_env_file(
        "# comment\nexport RESTIC_PASSWORD=\"pa ss\"\nRESTIC_REPOSITORY='local:/x'\n\nBAD\n"
    )
    assert parsed == {"RESTIC_PASSWORD": "pa ss", "RESTIC_REPOSITORY": "local:/x"}


def test_initialize_repository_requires_repository_and_password() -> None:
    with pytest.raises(_MODULE.BackupProvisionError, match="missing required keys"):
        _MODULE.initialize_repository({"RESTIC_PASSWORD": "x"})


def test_provision_from_file_raises_when_env_absent(tmp_path: Path) -> None:
    with pytest.raises(_MODULE.BackupProvisionError, match="not found"):
        _MODULE.provision_from_file(tmp_path / "absent.env")


class _ScriptedInit:
    """A ``run_init`` seam returning a scripted sequence of outcomes, then success.

    Each entry is ``(returncode, stderr)``; once the script is exhausted every
    further call succeeds (returncode 0). Records how many times it was called.
    """

    def __init__(self, outcomes: list[tuple[int, str]]) -> None:
        self._outcomes = list(outcomes)
        self.call_count = 0

    def __call__(self, env_values: dict[str, str]):  # type: ignore[no-untyped-def]
        self.call_count += 1
        if self._outcomes:
            returncode, stderr = self._outcomes.pop(0)
            return _MODULE._InitAttempt(returncode, "", stderr)
        return _MODULE._InitAttempt(0, "created restic repository", "")


def test_initialize_repository_retries_through_credential_propagation() -> None:
    # A just-minted R2 key is not yet active at the S3 edge: the first inits
    # fail with a signature/auth error, then it goes live. The web-create path
    # runs this init immediately, so it must ride out that window rather than
    # fail the whole create.
    scripted = _ScriptedInit(
        [
            (
                1,
                "Fatal: The request signature we calculated does not match the signature you provided",
            ),
            (1, "Fatal: InvalidAccessKeyId"),
        ]
    )
    slept: list[float] = []

    created = _MODULE.initialize_repository(
        {"RESTIC_REPOSITORY": "s3:https://x/b", "RESTIC_PASSWORD": "pw"},
        run_init=scripted,
        sleep=slept.append,
        monotonic=lambda: 0.0,
    )

    assert created is True
    assert scripted.call_count == 3
    assert len(slept) == 2


def test_initialize_repository_surfaces_a_persistent_failure_after_the_deadline() -> (
    None
):
    # A transient-looking auth error that never clears must still fail once the
    # retry window is exhausted (not loop forever). A fake clock jumps past the
    # deadline after the first wait.
    scripted = _ScriptedInit([(1, "Fatal: InvalidAccessKeyId")] * 50)
    clock = {"now": 0.0}

    def _advancing_sleep(seconds: float) -> None:
        clock["now"] += 1000.0

    with pytest.raises(_MODULE.BackupProvisionError, match="restic init failed"):
        _MODULE.initialize_repository(
            {"RESTIC_REPOSITORY": "s3:https://x/b", "RESTIC_PASSWORD": "pw"},
            run_init=scripted,
            sleep=_advancing_sleep,
            monotonic=lambda: clock["now"],
        )
    # One failing attempt, one wait, then the deadline check stops it.
    assert scripted.call_count == 2


def test_initialize_repository_does_not_retry_a_non_auth_failure() -> None:
    # A non-transient error (e.g. a malformed repository) fails immediately --
    # retrying it would just waste the whole window.
    scripted = _ScriptedInit(
        [(1, "Fatal: unable to open config file: bucket does not exist")]
    )
    slept: list[float] = []

    with pytest.raises(_MODULE.BackupProvisionError, match="bucket does not exist"):
        _MODULE.initialize_repository(
            {"RESTIC_REPOSITORY": "s3:https://x/b", "RESTIC_PASSWORD": "pw"},
            run_init=scripted,
            sleep=slept.append,
            monotonic=lambda: 0.0,
        )
    assert scripted.call_count == 1
    assert slept == []


@pytest.mark.skipif(
    shutil.which("restic") is None, reason="restic binary not installed"
)
def test_initialize_repository_is_idempotent_against_a_local_repo(
    tmp_path: Path,
) -> None:
    repo_dir = tmp_path / f"repo-{uuid.uuid4().hex}"
    env_values = {
        "RESTIC_REPOSITORY": f"local:{repo_dir}",
        "RESTIC_PASSWORD": uuid.uuid4().hex,
    }

    assert _MODULE.initialize_repository(env_values) is True
    # A second init against the same repository reports "already initialized".
    assert _MODULE.initialize_repository(env_values) is False

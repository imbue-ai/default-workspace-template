"""Fast-path ``mngr create`` against a real pre-baked pool slice.

The layer where remote-workspace creation actually happens for users: sign in
to the connector as a real account, run the same ``mngr create`` invocation the
desktop client runs for imbue_cloud (``--reuse``, ``main`` + ``imbue_cloud``
templates, ``fast_mode=require``), and assert the pre-baked slice is adopted
into a live workspace whose container answers commands. ``mngr destroy`` then
wipes the workspace (lease release is deliberately deferred to mngr's GC), and
the test drives the same explicit ``hosts release`` path GC would take,
proving the lease disappears from the connector.

Requires the run's pre-baked pool (specs/remote-workspaces-in-ci.md): the
lease request pins the stamped ``(repo_url, repo_branch_or_tag)`` pair from the
bake stage, so this test can only ever adopt this run's own bake. Consumes one
slice.
"""

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
import tomlkit
from loguru import logger

from imbue.minds.deployment_tests.data_types import DefaultWorkspaceTemplateRef
from imbue.minds.deployment_tests.data_types import DeploymentEnvsConfig
from imbue.minds.deployment_tests.data_types import SharedEnvHandle
from imbue.minds.deployment_tests.data_types import VerifiedUserHandle
from imbue.minds.deployment_tests.helpers import wait_for_env_ready
from imbue.minds.deployment_tests.testing import handle_no_pool_capacity
from imbue.minds.deployment_tests.testing import require_pool_info
from imbue.mngr.utils.polling import poll_for_value
from imbue.mngr.utils.testing import get_short_random_string

pytestmark = [pytest.mark.release, pytest.mark.minds_services]

_SIGNIN_TIMEOUT_SECONDS = 120
# The fast path adopts a pre-baked agent, but adoption still rotates both
# endpoints' sshd host keys and installs the in-VM reconciler over SSH.
_CREATE_TIMEOUT_SECONDS = 1200
_EXEC_TIMEOUT_SECONDS = 300
_DESTROY_TIMEOUT_SECONDS = 900
_HTTP_TIMEOUT_SECONDS = 60.0
# Bounds the post-teardown poll for the lease to vanish: the explicit
# `hosts release` destroys the slice VM on the box before reporting success,
# and the connector listing may lag -- allow a few minutes end to end.
_RELEASE_DEADLINE_SECONDS = 300.0
# Marker the imbue_cloud provider logs when the adopt path is taken; asserting
# on it pins that the test exercised the fast path rather than a silent
# fall-back rebuild.
_FAST_PATH_LOG_MARKER = "FAST PATH"
_PROVIDER_INSTANCE_NAME = "imbue_cloud_citest"


def _run(
    command: list[str],
    *,
    cwd: Path | None = None,
    timeout: int,
    env: dict[str, str] | None = None,
    logged_command: str | None = None,
) -> subprocess.CompletedProcess[str]:
    logger.info("Running: {}", " ".join(command) if logged_command is None else logged_command)
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False, env=env)


def _prepare_template_clone(source_worktree: Path, *, connector_url: str, account_email: str) -> Path:
    """Clone the orchestrator-prepared template checkout and wire it for imbue_cloud creates.

    The clone gets ``is_allowed_in_pytest = true`` (mngr's config guard refuses
    to run under PYTEST_CURRENT_TEST otherwise) plus the per-account
    ``imbue_cloud`` provider-instance block the create address targets -- the
    same dynamic entry minds writes for a signed-in account.
    """
    clone_target = Path(tempfile.mkdtemp(prefix="fastpath-e2e-dwt-")) / "default-workspace-template"
    # file:// (not a bare path) forces a real transport copy, fully decoupling
    # the clone from the source checkout's object store.
    clone = _run(["git", "clone", f"file://{source_worktree}", str(clone_target)], timeout=600)
    assert clone.returncode == 0, f"template clone failed: {clone.stderr}"
    settings_path = clone_target / ".mngr" / "settings.toml"
    doc = tomlkit.parse(settings_path.read_text())
    doc["is_allowed_in_pytest"] = True
    providers = doc.setdefault("providers", tomlkit.table())
    instance = tomlkit.table()
    instance["backend"] = "imbue_cloud"
    instance["account"] = account_email
    instance["connector_url"] = connector_url
    providers[_PROVIDER_INSTANCE_NAME] = instance
    settings_path.write_text(tomlkit.dumps(doc))
    return clone_target


def _build_mngr_env(template_path: Path) -> dict[str, str]:
    # The pytest isolation fixture sets MNGR_ROOT_NAME to a per-test value, which
    # makes mngr resolve project config at .<root_name>/ instead of the clone's
    # .mngr/ -- point the project config dir at the clone explicitly (same
    # pattern as test_litellm_via_workspace).
    env = dict(os.environ)
    env["MNGR_PROJECT_CONFIG_DIR"] = str(template_path / ".mngr")
    return env


def _parse_created_event(stdout: str) -> tuple[str, str]:
    agent_id, host_id = "", ""
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("event") == "created":
            agent_id = str(event.get("agent_id", ""))
            host_id = str(event.get("host_id", ""))
    assert agent_id and host_id, f"mngr create emitted no created event: {stdout[-2000:]}"
    return agent_id, host_id


def _auth_header(user: VerifiedUserHandle) -> dict[str, str]:
    return {"Authorization": f"Bearer {user.session_token.get_secret_value()}"}


def _list_leased_host_db_ids_or_none(connector_url: str, user: VerifiedUserHandle) -> list[str] | None:
    """The user's leased host db ids via the connector, or None when the listing cannot be read.

    For the teardown path and the release poll: a transient connector error
    there must not mask the test body's real failure (or abort a poll that
    would succeed on the next attempt), so it is logged and reported as None
    instead of raising.
    """
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            listing = client.get(f"{connector_url}/hosts", headers=_auth_header(user))
    except httpx.HTTPError as exc:
        logger.warning("/hosts listing failed (treated as unreadable): {}", exc)
        return None
    if listing.status_code != 200:
        logger.warning("/hosts listing failed (treated as unreadable): {} {}", listing.status_code, listing.text[:300])
        return None
    return [str(entry["host_db_id"]) for entry in listing.json()]


def _list_leased_host_db_ids(connector_url: str, user: VerifiedUserHandle) -> list[str]:
    leased_host_db_ids = _list_leased_host_db_ids_or_none(connector_url, user)
    assert leased_host_db_ids is not None, "/hosts listing failed (see the warning above)"
    return leased_host_db_ids


@pytest.mark.timeout(2700)
def test_fast_path_create_adopts_baked_slice_and_destroy_releases_lease(
    shared_env: Callable[[str], SharedEnvHandle],
    verified_user: VerifiedUserHandle,
    default_workspace_template_ref: DefaultWorkspaceTemplateRef,
    deployment_envs_config: DeploymentEnvsConfig,
) -> None:
    env = shared_env("default")
    wait_for_env_ready(env)
    connector_url = str(env.urls.connector_url).rstrip("/")
    pool = require_pool_info(deployment_envs_config)
    if default_workspace_template_ref.worktree_path is None:
        pytest.skip("No local template worktree available; the create must run from a template checkout")

    template_path = _prepare_template_clone(
        default_workspace_template_ref.worktree_path,
        connector_url=connector_url,
        account_email=str(verified_user.email),
    )
    mngr_env = _build_mngr_env(template_path)
    host_name = f"fastpath-{get_short_random_string()}"
    address = f"system-services@{host_name}.{_PROVIDER_INSTANCE_NAME}"
    try:
        # `mngr imbue_cloud auth signin` requires an already-initialized mngr
        # root config (it refuses to create one itself), so run a cheap mngr
        # command first purely for its config-initialization side effect. Its
        # exit code is deliberately ignored: pre-signin, the imbue_cloud
        # provider's discovery errors by design (no session yet), which makes
        # `mngr list` exit non-zero even with `--on-error continue`. The
        # signin below fails loudly if initialization did not happen.
        _run(
            ["mngr", "list", "--format", "json", "--on-error", "continue"],
            cwd=template_path,
            timeout=_SIGNIN_TIMEOUT_SECONDS,
            env=mngr_env,
        )

        # Sign the account in headlessly; the session persists in this test's
        # isolated mngr state and authenticates the provider's lease call.
        signin = _run(
            [
                "mngr",
                "imbue_cloud",
                "auth",
                "signin",
                "--account",
                str(verified_user.email),
                "--password",
                verified_user.password.get_secret_value(),
                "--connector-url",
                connector_url,
            ],
            cwd=template_path,
            timeout=_SIGNIN_TIMEOUT_SECONDS,
            env=mngr_env,
            logged_command=f"mngr imbue_cloud auth signin --account {verified_user.email} --password <redacted>",
        )
        assert signin.returncode == 0, f"mngr imbue_cloud auth signin failed: {signin.stderr[-1000:]}"

        # The same create the desktop client runs for imbue_cloud (see
        # agent_creator.py): --reuse for the baked system-services agent,
        # fast_mode=require so a missing exact match FAILS rather than silently
        # rebuilding, and the (repo_url, repo_branch_or_tag) identity pair
        # pinned to this run's bake.
        create = _run(
            [
                "mngr",
                "create",
                address,
                "--new-host",
                "--reuse",
                "--no-connect",
                "--format",
                "jsonl",
                "--label",
                "is_primary=true",
                "--branch",
                f":mngr/{host_name}",
                "--template",
                "main",
                "--template",
                "imbue_cloud",
                "-b",
                "fast_mode=require",
                "-b",
                f"repo_url={pool.repo_url}",
                "-b",
                f"repo_branch_or_tag={pool.repo_branch_or_tag}",
            ],
            cwd=template_path,
            timeout=_CREATE_TIMEOUT_SECONDS,
            env=mngr_env,
        )
        if create.returncode != 0 and "no pool host exactly matches" in create.stderr.lower():
            handle_no_pool_capacity("fast-path create found no matching pre-baked slice")
        assert create.returncode == 0, f"mngr create failed: {create.stderr[-3000:]}"
        assert _FAST_PATH_LOG_MARKER in create.stderr, (
            f"create succeeded but never logged the fast (adopt) path: {create.stderr[-2000:]}"
        )
        _agent_id, _host_id = _parse_created_event(create.stdout)

        # The lease is visible to the account through the connector, and the
        # adopted workspace's container actually executes commands.
        leased_host_db_ids = _list_leased_host_db_ids(connector_url, verified_user)
        assert leased_host_db_ids, "no lease visible via the connector after a successful fast-path create"
        probe_token = f"fastpath-ok-{get_short_random_string()}"
        probe = _run(
            ["mngr", "exec", address, f"echo {probe_token}"],
            cwd=template_path,
            timeout=_EXEC_TIMEOUT_SECONDS,
            env=mngr_env,
        )
        assert probe.returncode == 0, f"mngr exec on the adopted workspace failed: {probe.stderr[-1000:]}"
        assert probe_token in probe.stdout, f"exec output missing the probe token: {probe.stdout[-500:]}"
    finally:
        # Destroy unconditionally: even a create that failed partway may have
        # leased a slice. `mngr destroy` wipes the workspace; the LEASE is
        # deliberately not released here -- mngr's GC releases it after the
        # destroyed-host grace period (see specs/detached-destroy-flow, "No
        # imbue_cloud lease release") -- so the explicit release below is the
        # same `hosts release` path GC takes, run eagerly so the test (and its
        # slice slot) does not wait out the grace period.
        destroy = _run(
            ["mngr", "destroy", address, "--force"],
            cwd=template_path,
            timeout=_DESTROY_TIMEOUT_SECONDS,
            env=mngr_env,
        )
        if destroy.returncode != 0:
            logger.warning("Workspace teardown failed (sweeps will reclaim it): {}", destroy.stderr[-500:])
        # The tolerant listing: raising here would mask the test body's failure.
        for leased_host_db_id in _list_leased_host_db_ids_or_none(connector_url, verified_user) or []:
            release = _run(
                ["mngr", "imbue_cloud", "hosts", "release", leased_host_db_id, "--connector-url", connector_url],
                cwd=template_path,
                timeout=_DESTROY_TIMEOUT_SECONDS,
                env=mngr_env,
            )
            if release.returncode != 0:
                logger.warning("Lease release failed (sweeps will reclaim it): {}", release.stderr[-500:])
        shutil.rmtree(template_path.parent, ignore_errors=True)

    # The release is terminal: the lease disappears from the connector (the
    # release endpoint destroys the slice VM before reporting success; a short
    # poll absorbs connector-side read lag).
    def read_empty_listing_or_none() -> bool | None:
        # Only a confirmed-empty listing ends the poll; a transient listing
        # failure (None) retries within the deadline like a non-empty one.
        return True if _list_leased_host_db_ids_or_none(connector_url, verified_user) == [] else None

    released, _poll_count, _elapsed = poll_for_value(
        read_empty_listing_or_none, timeout=_RELEASE_DEADLINE_SECONDS, poll_interval=10.0
    )
    assert released, (
        f"the lease is still listed by the connector {_RELEASE_DEADLINE_SECONDS:.0f}s after `hosts release`"
    )

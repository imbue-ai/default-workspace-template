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

from collections.abc import Callable

import pytest

from imbue.minds.deployment_tests.data_types import DefaultWorkspaceTemplateRef
from imbue.minds.deployment_tests.data_types import DeploymentEnvsConfig
from imbue.minds.deployment_tests.data_types import SharedEnvHandle
from imbue.minds.deployment_tests.data_types import VerifiedUserHandle
from imbue.minds.deployment_tests.helpers import wait_for_env_ready
from imbue.minds.deployment_tests.testing import POOL_PROVIDER_INSTANCE_NAME
from imbue.minds.deployment_tests.testing import build_mngr_env
from imbue.minds.deployment_tests.testing import handle_no_pool_capacity
from imbue.minds.deployment_tests.testing import initialize_and_sign_in
from imbue.minds.deployment_tests.testing import list_leased_host_db_ids
from imbue.minds.deployment_tests.testing import list_leased_host_db_ids_or_none
from imbue.minds.deployment_tests.testing import parse_created_event
from imbue.minds.deployment_tests.testing import prepare_template_clone
from imbue.minds.deployment_tests.testing import require_pool_info
from imbue.minds.deployment_tests.testing import run_command
from imbue.minds.deployment_tests.testing import tear_down_pool_workspace
from imbue.mngr.utils.polling import poll_for_value
from imbue.mngr.utils.testing import get_short_random_string
from imbue.mngr_imbue_cloud.primitives import IMBUE_CLOUD_BACKEND_NAME

pytestmark = [pytest.mark.release, pytest.mark.minds_services]

# The fast path adopts a pre-baked agent, but adoption still rotates both
# endpoints' sshd host keys and installs the in-VM reconciler over SSH.
_CREATE_TIMEOUT_SECONDS = 1200
_EXEC_TIMEOUT_SECONDS = 300
# Bounds the post-teardown poll for the lease to vanish: the explicit
# `hosts release` destroys the slice VM on the box before reporting success,
# and the connector listing may lag -- allow a few minutes end to end.
_RELEASE_DEADLINE_SECONDS = 300.0
# Marker the imbue_cloud provider logs when the adopt path is taken; asserting
# on it pins that the test exercised the fast path rather than a silent
# fall-back rebuild.
_FAST_PATH_LOG_MARKER = "FAST PATH"


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

    # Only the account binding: the fast path adopts the pre-baked container
    # as-is, so nothing else on the block would reach it (the slow-path test
    # passes the full block minds writes, which its rebuild does read).
    template_path = prepare_template_clone(
        default_workspace_template_ref.worktree_path,
        provider_instance_name=POOL_PROVIDER_INSTANCE_NAME,
        provider_block={
            "backend": IMBUE_CLOUD_BACKEND_NAME,
            "account": str(verified_user.email),
            "connector_url": connector_url,
        },
    )
    mngr_env = build_mngr_env(template_path)
    host_name = f"fastpath-{get_short_random_string()}"
    address = f"system-services@{host_name}.{POOL_PROVIDER_INSTANCE_NAME}"
    try:
        initialize_and_sign_in(
            template_path,
            mngr_env,
            account_email=str(verified_user.email),
            password=verified_user.password.get_secret_value(),
            connector_url=connector_url,
        )

        # The same create the desktop client runs for imbue_cloud (see
        # agent_creator.py): --reuse for the baked system-services agent,
        # fast_mode=require so a missing exact match FAILS rather than silently
        # rebuilding, and the (repo_url, repo_branch_or_tag) identity pair
        # pinned to this run's bake.
        create = run_command(
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
        parse_created_event(create.stdout)

        # The lease is visible to the account through the connector, and the
        # adopted workspace's container actually executes commands.
        leased_host_db_ids = list_leased_host_db_ids(connector_url, verified_user)
        assert leased_host_db_ids, "no lease visible via the connector after a successful fast-path create"
        probe_token = f"fastpath-ok-{get_short_random_string()}"
        probe = run_command(
            ["mngr", "exec", address, f"echo {probe_token}"],
            cwd=template_path,
            timeout=_EXEC_TIMEOUT_SECONDS,
            env=mngr_env,
        )
        assert probe.returncode == 0, f"mngr exec on the adopted workspace failed: {probe.stderr[-1000:]}"
        assert probe_token in probe.stdout, f"exec output missing the probe token: {probe.stdout[-500:]}"
    finally:
        tear_down_pool_workspace(address, template_path, mngr_env, connector_url=connector_url, user=verified_user)

    # The release is terminal: the lease disappears from the connector (the
    # release endpoint destroys the slice VM before reporting success; a short
    # poll absorbs connector-side read lag).
    def read_empty_listing_or_none() -> bool | None:
        # Only a confirmed-empty listing ends the poll; a transient listing
        # failure (None) retries within the deadline like a non-empty one.
        listing = list_leased_host_db_ids_or_none(connector_url, verified_user)
        return True if listing == [] else None

    released, _poll_count, _elapsed = poll_for_value(
        read_empty_listing_or_none, timeout=_RELEASE_DEADLINE_SECONDS, poll_interval=10.0
    )
    assert released, (
        f"the lease is still listed by the connector {_RELEASE_DEADLINE_SECONDS:.0f}s after `hosts release`"
    )

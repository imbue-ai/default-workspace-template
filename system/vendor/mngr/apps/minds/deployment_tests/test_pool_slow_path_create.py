"""Slow-path (rebuild) ``mngr create`` against a real pool slice.

The path minds falls back to whenever no pre-baked pool host matches the
requested template tag (``fast_mode=prevent``, also mngr's default): lease any
adequately-sized slice, tear down its baked container, and rebuild it from the
DEFAULT_WORKSPACE_TEMPLATE Dockerfile. Unlike the fast path, which adopts the
baked container as-is, the rebuild carves the container's volume itself -- from
the per-account provider block -- so this is the only place the account block's
user-data layout knobs are exercised end to end.

The assertion is the layout: ``/home/user`` must resolve onto the persistent
volume's ``home/`` subtree. When the rebuild does not forward
``volume_home_path``, the create still succeeds and the workspace still runs --
it just lands on the legacy ``host_dir/``-only volume, with the whole home tree
(the workspace checkout, the user's apps and skills, dotfiles) in the
container's writable layer, and host_backup failing every tick on a missing
``home``. Nothing short of reading the realized layout catches that.

Consumes one slice.
"""

from collections.abc import Callable

import pytest

from imbue.minds.deployment_tests.data_types import DefaultWorkspaceTemplateRef
from imbue.minds.deployment_tests.data_types import SharedEnvHandle
from imbue.minds.deployment_tests.data_types import VerifiedUserHandle
from imbue.minds.deployment_tests.helpers import wait_for_env_ready
from imbue.minds.deployment_tests.testing import POOL_PROVIDER_INSTANCE_NAME
from imbue.minds.deployment_tests.testing import build_mngr_env
from imbue.minds.deployment_tests.testing import handle_no_pool_capacity
from imbue.minds.deployment_tests.testing import initialize_and_sign_in
from imbue.minds.deployment_tests.testing import list_leased_host_db_ids
from imbue.minds.deployment_tests.testing import parse_created_event
from imbue.minds.deployment_tests.testing import parse_error_event_class
from imbue.minds.deployment_tests.testing import prepare_template_clone
from imbue.minds.deployment_tests.testing import run_command
from imbue.minds.deployment_tests.testing import tear_down_pool_workspace
from imbue.minds.mngr_settings.provider_blocks import WORKSPACE_HOST_LOG_DIR
from imbue.minds.mngr_settings.provider_blocks import WORKSPACE_VOLUME_HOME_PATH
from imbue.minds.mngr_settings.provider_blocks import imbue_cloud_account_provider_block
from imbue.mngr.utils.testing import get_short_random_string
from imbue.mngr_imbue_cloud.errors import ImbueCloudLeaseUnavailableError
from imbue.mngr_vps.container_setup import HOME_SUBPATH
from imbue.mngr_vps.container_setup import HOST_VOLUME_MOUNT_PATH

# rsync: the rebuild uploads the workspace build context to the leased host,
# which the resource guard blocks unmarked (the fast path adopts a baked
# container and uploads nothing, so it needs no such mark).
pytestmark = [pytest.mark.release, pytest.mark.minds_services, pytest.mark.rsync]

# The slow path tears the baked container down and builds the template's
# Dockerfile on the slice, then runs full client-side setup -- a real image
# build, well beyond the fast path's adopt-and-rotate-keys budget.
_CREATE_TIMEOUT_SECONDS = 3000
_EXEC_TIMEOUT_SECONDS = 300
# Marker the imbue_cloud provider logs when the rebuild path is taken; asserting
# on it pins that the test exercised the slow path rather than silently adopting.
# The provider logs a "SLOW PATH" line before leasing too, so match the one it
# logs only after the container is back up.
_SLOW_PATH_LOG_MARKER = "SLOW PATH: rebuilt container on leased host"
# The ``error_class`` the provider surfaces on a genuinely empty pool. Read off
# the class rather than spelled out, because that is literally what
# ``mngr --format jsonl`` emits (``type(error).__name__``).
_LEASE_UNAVAILABLE_ERROR_CLASS = ImbueCloudLeaseUnavailableError.__name__

# Where the rebuilt container's home tree must live: the unified host volume's
# home/ subdirectory, which is what makes it survive the container and what
# host_backup reads its `home` subpath from.
_HOME_ON_VOLUME = f"{HOST_VOLUME_MOUNT_PATH}/{HOME_SUBPATH}"
# One round trip, because each `mngr exec` is a fresh SSH session: resolve the
# home path, then confirm mngr's own data dir sits inside it (host_dir is a
# plain directory under the home tree in this layout, not a second symlink) and
# that the configured host_log_dir was created.
_LAYOUT_PROBE = (
    f'echo "home_resolves_to=$(readlink -f {WORKSPACE_VOLUME_HOME_PATH})"; '
    f'echo "mngr_data_on_volume=$(test -d {_HOME_ON_VOLUME}/.mngr && echo yes || echo no)"; '
    f'echo "host_log_dir=$(test -d {WORKSPACE_HOST_LOG_DIR} && echo yes || echo no)"'
)


@pytest.mark.timeout(4500)
def test_slow_path_rebuild_carves_the_home_volume_layout(
    shared_env: Callable[[str], SharedEnvHandle],
    verified_user: VerifiedUserHandle,
    default_workspace_template_ref: DefaultWorkspaceTemplateRef,
) -> None:
    env = shared_env("default")
    wait_for_env_ready(env)
    connector_url = str(env.urls.connector_url).rstrip("/")
    if default_workspace_template_ref.worktree_path is None:
        pytest.skip("No local template worktree available; the create must run from a template checkout")

    # The full per-account block minds writes, not just the account binding:
    # the rebuild reads the layout and hardening knobs off it.
    template_path = prepare_template_clone(
        default_workspace_template_ref.worktree_path,
        provider_instance_name=POOL_PROVIDER_INSTANCE_NAME,
        provider_block=imbue_cloud_account_provider_block(email=str(verified_user.email), connector_url=connector_url),
    )
    mngr_env = build_mngr_env(template_path)
    host_name = f"slowpath-{get_short_random_string()}"
    address = f"system-services@{host_name}.{POOL_PROVIDER_INSTANCE_NAME}"
    try:
        initialize_and_sign_in(
            template_path,
            mngr_env,
            account_email=str(verified_user.email),
            password=verified_user.password.get_secret_value(),
            connector_url=connector_url,
        )

        # The create the desktop client falls back to (see agent_creator.py's
        # fast_mode retry loop). The repo identity minds also passes is left off
        # deliberately: the slow path relaxes both identity attributes away
        # before leasing, so pinning them here would suggest the lease depends
        # on this run's own bake when it does not -- any free slice will do.
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
                "fast_mode=prevent",
            ],
            cwd=template_path,
            timeout=_CREATE_TIMEOUT_SECONDS,
            env=mngr_env,
        )
        # The lease itself reports an empty pool, so there is no upstream
        # capacity precheck here. Read the class off the structured stdout
        # event rather than stderr, which carries only the connector-owned
        # 503 text with no type on it.
        if create.returncode != 0 and parse_error_event_class(create.stdout) == _LEASE_UNAVAILABLE_ERROR_CLASS:
            handle_no_pool_capacity("slow-path create found no leasable slice")
        assert create.returncode == 0, f"mngr create failed: {create.stderr[-3000:]}"
        assert _SLOW_PATH_LOG_MARKER in create.stderr, (
            f"create succeeded but never logged the slow (rebuild) path: {create.stderr[-2000:]}"
        )
        parse_created_event(create.stdout)
        assert list_leased_host_db_ids(connector_url, verified_user), (
            "no lease visible via the connector after a successful slow-path create"
        )

        probe = run_command(
            ["mngr", "exec", address, _LAYOUT_PROBE],
            cwd=template_path,
            timeout=_EXEC_TIMEOUT_SECONDS,
            env=mngr_env,
        )
        assert probe.returncode == 0, f"mngr exec on the rebuilt workspace failed: {probe.stderr[-1000:]}"
        layout_hint = (
            f"the rebuild carved the legacy {HOST_VOLUME_MOUNT_PATH} layout instead of forwarding the account "
            f"block's volume_home_path; probe output:\n{probe.stdout[-1000:]}"
        )
        assert f"home_resolves_to={_HOME_ON_VOLUME}" in probe.stdout, (
            f"{WORKSPACE_VOLUME_HOME_PATH} does not resolve onto the volume -- {layout_hint}"
        )
        assert "mngr_data_on_volume=yes" in probe.stdout, (
            f"mngr's data dir is not inside the home tree -- {layout_hint}"
        )
        assert "host_log_dir=yes" in probe.stdout, (
            f"{WORKSPACE_HOST_LOG_DIR} was not created, so host_log_dir did not reach the rebuild -- {layout_hint}"
        )
    finally:
        tear_down_pool_workspace(address, template_path, mngr_env, connector_url=connector_url, user=verified_user)

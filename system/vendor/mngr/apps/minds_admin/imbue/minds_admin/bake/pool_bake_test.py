import json
import os
import time

import pytest

from imbue.minds_admin.bake.pool_bake import BAKED_SERVICES_AGENT_NAME
from imbue.minds_admin.bake.pool_bake import BakedPoolHost
from imbue.minds_admin.bake.pool_bake import DEFAULT_WORKSPACE_TEMPLATE_BAKE_TEMPLATES
from imbue.minds_admin.bake.pool_bake import EPHEMERAL_BAKE_MNGR_PREFIX
from imbue.minds_admin.bake.pool_bake import PoolBakeError
from imbue.minds_admin.bake.pool_bake import bake_namespace_parent_dir
from imbue.minds_admin.bake.pool_bake import build_pool_create_command
from imbue.minds_admin.bake.pool_bake import ephemeral_bake_namespace
from imbue.minds_admin.bake.pool_bake import finalize_baked_pool_host
from imbue.minds_admin.bake.pool_bake import parse_baked_host
from imbue.minds_admin.bake.pool_bake import sweep_stale_bake_namespaces
from imbue.minds_admin.bake.pool_bake import verify_only_primary_agents_baked
from imbue.minds_admin.bake.pool_bake import wait_for_env_converge


class _ScriptedRunner:
    """A ContainerCommandRunner that returns a scripted ``(rc, out, err)`` per step label.

    Lets finalize_baked_pool_host be unit-tested without a real container: each call
    records its (label, command) and returns the response scripted for that label
    (default ``(0, "", "")``).
    """

    def __init__(self, responses: dict[str, tuple[int | None, str, str]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str]] = []

    def __call__(
        self, baked: BakedPoolHost, label: str, command: str, timeout_seconds: float
    ) -> tuple[int | None, str, str]:
        self.calls.append((label, command))
        return self.responses.get(label, (0, "", ""))


def _baked() -> BakedPoolHost:
    return BakedPoolHost(agent_id="a", host_id="h", host_name="slice-x", ssh_host="1.2.3.4", ssh_port=22001)


def test_finalize_hardens_and_clears_identity_and_does_nothing_else() -> None:
    """Finalize runs exactly these two steps, in this order.

    In particular there is no chat-agent teardown: DEFAULT_WORKSPACE_TEMPLATE creates no chat
    at boot (a chat binds to a provider account when it is CREATED and nothing rebinds it, so a
    pre-sign-in chat could never take a turn), so finalize has nothing to wait for or destroy.
    """
    runner = _ScriptedRunner({})
    finalize_baked_pool_host(runner, _baked(), host_name="slice-x")
    assert [label for label, _cmd in runner.calls] == ["sshd-harden", "git-identity-reset"]


def test_finalize_clears_baked_git_identity() -> None:
    # The bake copies the operator's git identity into the workspace checkout; finalize must
    # unset it so adopting users' agents don't inherit the baker as their commit author.
    #
    # What re-supplies it on adoption is the template bootstrap, which sets an only-if-unset
    # identity on EVERY boot rather than behind a first-run signal -- so this unset is always
    # recovered from, whatever else the adopted workspace does or does not do on first start.
    runner = _ScriptedRunner({})
    finalize_baked_pool_host(runner, _baked(), host_name="slice-x")
    reset_cmd = next(cmd for label, cmd in runner.calls if label == "git-identity-reset")
    assert "git -C /home/user/workspace config --local --unset user.name" in reset_cmd
    assert "git -C /home/user/workspace config --local --unset user.email" in reset_cmd
    # It does not substitute any hardcoded identity value.
    assert "minds-bootstrap" not in reset_cmd
    # An already-absent key (git config --unset exit 5) is tolerated, not a failure.
    assert "[ $? -eq 5 ]" in reset_cmd


def test_finalize_git_identity_reset_failure_is_best_effort() -> None:
    # The rewrite hook is the authoritative per-agent attribution, so a failed identity reset
    # is logged rather than failing the bake.
    runner = _ScriptedRunner({"git-identity-reset": (1, "", "boom")})
    finalize_baked_pool_host(runner, _baked(), host_name="slice-x")
    assert "git-identity-reset" in [label for label, _cmd in runner.calls]


def test_finalize_sshd_harden_failure_is_best_effort() -> None:
    # sshd-harden is best-effort: a failure is logged, not fatal, and the rest still runs.
    runner = _ScriptedRunner({"sshd-harden": (1, "", "boom")})
    finalize_baked_pool_host(runner, _baked(), host_name="slice-x")
    assert "git-identity-reset" in [label for label, _cmd in runner.calls]


def test_wait_for_env_converge_polls_for_rootfs_stamp_or_finished_oneshot() -> None:
    runner = _ScriptedRunner({"env-converge-wait": (0, "converged", "")})
    wait_for_env_converge(runner, _baked(), host_name="slice-x", timeout_seconds=5)
    assert [label for label, _cmd in runner.calls] == ["env-converge-wait"]
    command = runner.calls[0][1]
    # The poll's success condition is the slow phase's own final step (the rootfs stamp,
    # written after the record files), so waiting on it covers the whole phase -- not just
    # the Fortress binary, which a cached image can carry while apt is still running.
    assert "test -e /var/lib/minds/env-converge/rootfs-id" in command
    # The bail-early condition asks supervisord about the one-shot itself: EXITED/FATAL
    # (crashed or finished) and an unknown program both stop the wait, while a supervisord
    # that is not up yet (socket error, matching neither) keeps it polling.
    assert "supervisorctl status env-converge" in command
    assert "EXITED|FATAL|no such process" in command
    assert "timeout 5" in command


def test_wait_for_env_converge_is_best_effort_when_oneshot_exited_without_stamp() -> None:
    # A converge that crashed before stamping must not fail the bake; it retries on lease.
    runner = _ScriptedRunner({"env-converge-wait": (0, "exited-without-stamp", "")})
    wait_for_env_converge(runner, _baked(), host_name="slice-x", timeout_seconds=5)


def test_wait_for_env_converge_is_best_effort_on_timeout() -> None:
    # Hitting the cap (timeout exit 124) must not fail the bake; it retries on lease.
    runner = _ScriptedRunner({"env-converge-wait": (124, "", "")})
    wait_for_env_converge(runner, _baked(), host_name="slice-x", timeout_seconds=5)


def test_wait_for_env_converge_is_best_effort_on_transport_error() -> None:
    runner = _ScriptedRunner({"env-converge-wait": (255, "", "ssh: connect failed")})
    wait_for_env_converge(runner, _baked(), host_name="slice-x", timeout_seconds=5)


def _agent_listing_json(agents: list[dict[str, object]], errors: list[dict[str, object]] | None = None) -> str:
    return json.dumps({"agents": agents, "errors": errors or []})


def test_verify_only_primary_agents_baked_passes_with_only_the_primary_agent() -> None:
    listing = _agent_listing_json(
        [{"id": "agent-1", "name": "system-services", "labels": {"is_primary": "true", "user_created": "true"}}]
    )
    runner = _ScriptedRunner({"verify-agents": (0, listing, "")})
    verify_only_primary_agents_baked(runner, _baked(), host_name="slice-x")
    assert [label for label, _cmd in runner.calls] == ["verify-agents"]
    # The check reads the end state through the vendored mngr, from the workspace checkout.
    command = runner.calls[0][1]
    assert "cd /home/user/workspace" in command
    assert "uv run mngr list --format json" in command


def test_verify_only_primary_agents_baked_fails_the_bake_on_a_leaked_chat_agent() -> None:
    # The historical leak: an old-tag bootstrap created a boot chat, and the teardown's
    # wrong-name `mngr destroy --force` silently missed it. The end-state check must fail
    # the bake loudly instead (this is also what refuses old-tag bakes).
    listing = _agent_listing_json(
        [
            {"id": "agent-1", "name": "system-services", "labels": {"is_primary": "true"}},
            {"id": "agent-2", "name": "Chat-1", "labels": {"display_name": "Chat 1"}},
        ]
    )
    runner = _ScriptedRunner({"verify-agents": (0, listing, "")})
    with pytest.raises(PoolBakeError, match="Chat-1"):
        verify_only_primary_agents_baked(runner, _baked(), host_name="slice-x")


def test_verify_only_primary_agents_baked_fails_when_the_listing_command_fails() -> None:
    runner = _ScriptedRunner({"verify-agents": (1, "", "boom")})
    with pytest.raises(PoolBakeError, match="could not list agents"):
        verify_only_primary_agents_baked(runner, _baked(), host_name="slice-x")


def test_verify_only_primary_agents_baked_fails_on_unparseable_output() -> None:
    runner = _ScriptedRunner({"verify-agents": (0, "not json at all", "")})
    with pytest.raises(PoolBakeError):
        verify_only_primary_agents_baked(runner, _baked(), host_name="slice-x")


def test_verify_only_primary_agents_baked_fails_on_a_malformed_agent_entry() -> None:
    # A non-dict agent entry means the listing cannot be trusted; it must raise
    # PoolBakeError (so the caller's rollback catches it), not crash with AttributeError.
    runner = _ScriptedRunner({"verify-agents": (0, '{"agents": ["garbage"], "errors": []}', "")})
    with pytest.raises(PoolBakeError, match="malformed agent entry"):
        verify_only_primary_agents_baked(runner, _baked(), host_name="slice-x")


def test_verify_only_primary_agents_baked_fails_on_discovery_errors() -> None:
    # A listing with discovery errors may be missing agents, so it can never prove the
    # host is clean.
    listing = _agent_listing_json(
        [{"id": "agent-1", "name": "system-services", "labels": {"is_primary": "true"}}],
        errors=[{"message": "provider unreachable"}],
    )
    runner = _ScriptedRunner({"verify-agents": (0, listing, "")})
    with pytest.raises(PoolBakeError, match="discovery errors"):
        verify_only_primary_agents_baked(runner, _baked(), host_name="slice-x")


def test_verify_only_primary_agents_baked_fails_on_an_empty_listing() -> None:
    # The parked services agent must still be visible; zero agents means the listing
    # itself is broken (e.g. mngr resolved the wrong host dir), not a clean host.
    runner = _ScriptedRunner({"verify-agents": (0, _agent_listing_json([]), "")})
    with pytest.raises(PoolBakeError, match="no agents"):
        verify_only_primary_agents_baked(runner, _baked(), host_name="slice-x")


def test_verify_only_primary_agents_baked_treats_a_missing_labels_dict_as_non_primary() -> None:
    listing = _agent_listing_json([{"id": "agent-2", "name": "mystery"}])
    runner = _ScriptedRunner({"verify-agents": (0, listing, "")})
    with pytest.raises(PoolBakeError, match="mystery"):
        verify_only_primary_agents_baked(runner, _baked(), host_name="slice-x")


def test_build_pool_create_command_targets_the_given_provider_with_default_workspace_templates() -> None:
    command = build_pool_create_command(
        provider_instance="imbue_cloud_slice",
        host_name="slice-abc",
        attributes_json='{"cpus": 3}',
        extra_args=["-S", "providers.imbue_cloud_slice.slice_vcpus=3"],
    )
    # Address carries the constant services agent name + per-bake host + provider.
    assert command[1] == f"{BAKED_SERVICES_AGENT_NAME}@slice-abc.imbue_cloud_slice"
    # Both DEFAULT_WORKSPACE_TEMPLATE bake templates are stacked, and the result is machine-readable.
    for template in DEFAULT_WORKSPACE_TEMPLATE_BAKE_TEMPLATES:
        assert template in command
    assert "--format" in command and "json" in command
    # The pool attributes ride along as a label, and extra args are appended verbatim.
    assert 'pool_attributes={"cpus": 3}' in command
    assert command[-2:] == ["-S", "providers.imbue_cloud_slice.slice_vcpus=3"]


def test_build_pool_create_command_for_ovh_appends_backend_args() -> None:
    command = build_pool_create_command(
        provider_instance="ovh",
        host_name="pool-xyz-host",
        attributes_json="{}",
        extra_args=["-b", "--ovh-datacenter=vin"],
    )
    assert command[1] == f"{BAKED_SERVICES_AGENT_NAME}@pool-xyz-host.ovh"
    assert command[-2:] == ["-b", "--ovh-datacenter=vin"]


def test_parse_baked_host_reads_all_fields_from_create_json() -> None:
    stdout = (
        "some build log line on stdout that is not json\n"
        + json.dumps(
            {
                "agent_id": "agent-1",
                "host_id": "host-1",
                "host_name": "slice-abc",
                "ssh_user": "root",
                "ssh_host": "15.0.0.1",
                "ssh_port": 22002,
                "ssh_key_path": "/keys/container_ssh_key",
                "outer_ssh_port": 22001,
            }
        )
        + "\n"
    )
    baked = parse_baked_host(stdout, host_name="slice-abc")
    assert baked.agent_id == "agent-1"
    assert baked.host_id == "host-1"
    assert baked.host_name == "slice-abc"
    assert baked.ssh_host == "15.0.0.1"
    assert baked.ssh_port == 22002
    assert baked.ssh_key_path == "/keys/container_ssh_key"
    assert baked.outer_ssh_port == 22001


def test_parse_baked_host_tolerates_absent_outer_port_for_ovh() -> None:
    # OVH has no separate outer/management sshd, so outer_ssh_port is absent.
    stdout = json.dumps({"agent_id": "a", "host_id": "h", "ssh_host": "vps.ovh.us", "ssh_port": 2222})
    baked = parse_baked_host(stdout, host_name="pool-1-host")
    assert baked.outer_ssh_port is None
    assert baked.ssh_port == 2222
    # host_name falls back to the bake's name when the JSON omits it.
    assert baked.host_name == "pool-1-host"


def test_parse_baked_host_raises_when_no_json_present() -> None:
    with pytest.raises(PoolBakeError):
        parse_baked_host("only logs here, no json object\n", host_name="x")


def test_parse_baked_host_raises_when_host_id_missing() -> None:
    with pytest.raises(PoolBakeError):
        parse_baked_host(json.dumps({"agent_id": "a"}), host_name="x")


# The namespace/sweep tests below rely on the package's autouse setup_test_mngr_env
# fixture (see conftest.py), which points HOME at a fresh per-test temp dir -- so
# bake_namespace_parent_dir() (derived from Path.home()) is always test-isolated.


def test_ephemeral_bake_namespace_is_deleted_on_clean_exit() -> None:
    with ephemeral_bake_namespace() as namespace:
        assert namespace.host_dir.is_dir()
        assert namespace.namespace_dir.parent == bake_namespace_parent_dir()
    assert not namespace.namespace_dir.exists()


def test_ephemeral_bake_namespace_is_retained_when_body_raises() -> None:
    with pytest.raises(PoolBakeError):
        with ephemeral_bake_namespace() as namespace:
            raise PoolBakeError("bake failed")
    assert namespace.namespace_dir.is_dir()


def test_ephemeral_bake_namespace_is_retained_on_system_exit() -> None:
    # A partial failure exits via `raise SystemExit(1)` inside the block (see
    # allocate_slices); retention must cover it even though it is a BaseException.
    with pytest.raises(SystemExit):
        with ephemeral_bake_namespace() as namespace:
            raise SystemExit(1)
    assert namespace.namespace_dir.is_dir()


def test_ephemeral_bake_namespace_env_points_inner_mngr_at_the_namespace() -> None:
    with ephemeral_bake_namespace() as namespace:
        env = namespace.to_subprocess_env()
    # Exactly the two namespace vars: MNGR_HOST_DIR relocates all local mngr state,
    # MNGR_PREFIX keeps resource naming inert. Both override inherited values via
    # run_mngr_command's env merge (extra_env is layered over os.environ).
    assert env == {
        "MNGR_HOST_DIR": str(namespace.host_dir),
        "MNGR_PREFIX": EPHEMERAL_BAKE_MNGR_PREFIX,
    }
    assert not EPHEMERAL_BAKE_MNGR_PREFIX.startswith("minds")


def test_ephemeral_bake_namespace_dirs_are_private() -> None:
    # The namespace accumulates baked containers' SSH private keys, so it must not
    # be group/world readable.
    with pytest.raises(PoolBakeError):
        with ephemeral_bake_namespace() as namespace:
            assert namespace.namespace_dir.stat().st_mode & 0o777 == 0o700
            assert namespace.host_dir.stat().st_mode & 0o777 == 0o700
            raise PoolBakeError("retain for the outer asserts")
    assert namespace.namespace_dir.stat().st_mode & 0o777 == 0o700


def test_sweep_stale_bake_namespaces_removes_only_dirs_past_the_window() -> None:
    parent = bake_namespace_parent_dir()
    parent.mkdir(parents=True)
    stale_dir = parent / "stale-namespace"
    stale_dir.mkdir()
    (stale_dir / "host_dir").mkdir()
    fresh_dir = parent / "fresh-namespace"
    fresh_dir.mkdir()
    stray_file = parent / "not-a-namespace.txt"
    stray_file.write_text("left alone")
    eight_days_ago = time.time() - 8 * 24 * 60 * 60
    os.utime(stale_dir, (eight_days_ago, eight_days_ago))
    os.utime(stray_file, (eight_days_ago, eight_days_ago))

    sweep_stale_bake_namespaces()

    assert not stale_dir.exists()
    assert fresh_dir.is_dir()
    assert stray_file.exists()


def test_sweep_stale_bake_namespaces_tolerates_a_missing_parent_dir() -> None:
    assert not bake_namespace_parent_dir().exists()
    sweep_stale_bake_namespaces()
    assert not bake_namespace_parent_dir().exists()

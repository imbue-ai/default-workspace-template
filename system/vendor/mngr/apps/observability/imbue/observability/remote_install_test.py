from imbue.observability.config_render import render_all_instance_artifacts
from imbue.observability.primitives import CollectorRole
from imbue.observability.remote_install import REMOTE_ARTIFACT_PATHS
from imbue.observability.remote_install import render_instance_install_script
from imbue.observability.remote_install import self_collector_config
from imbue.observability.testing import make_instance_config


def test_every_rendered_artifact_has_a_remote_destination() -> None:
    # The render and the install script must agree on the artifact set, or a
    # deploy would silently skip (or fail on) a config file.
    artifacts = render_all_instance_artifacts(make_instance_config(tier="production"))
    assert set(artifacts) == set(REMOTE_ARTIFACT_PATHS)


def test_install_script_pins_and_verifies_the_openobserve_release() -> None:
    script = render_instance_install_script()
    assert "openobserve-v0.92.2-linux-${openobserve_goarch}.tar.gz" in script
    assert "sha256sum -c -" in script
    assert "2b9d35034a6810a6a2043447055cfa493f9302c0402f5a83728efc9f848b68a9" in script
    assert "efa8d4593a99dbf9d94e26d854c2e7a789e03f7b89eff6c8882b973d09268dec" in script
    # Version-stamped idempotence: a re-run on a converged host downloads
    # nothing; a version bump re-downloads.
    assert "/usr/local/share/openobserve.version" in script


def test_install_script_keeps_secret_bearing_files_locked_down() -> None:
    script = render_instance_install_script()
    # The env file carries the root password, DSN, and R2 keys: root-only.
    assert 'install -m 0600 -o root -g root "/tmp/observability-staging/openobserve.env"' in script
    # The origin TLS material is group-readable by the caddy service user only.
    assert 'install -m 0640 -o root -g caddy "/tmp/observability-staging/origin.key"' in script


def test_install_script_runs_openobserve_as_a_dedicated_system_user() -> None:
    script = render_instance_install_script()
    assert "useradd --system" in script
    assert "install -d -m 0750 -o openobserve -g openobserve /var/lib/openobserve" in script


def test_install_script_waits_out_first_boot_before_installing() -> None:
    # The one-shot recipe deploys right after the VPS reports ACTIVE, but the
    # steps below need cloud-init's packages (caddy and its group, nftables).
    script = render_instance_install_script()
    assert "cloud-init status --wait" in script


def test_install_script_enables_and_restarts_all_services() -> None:
    script = render_instance_install_script()
    assert "systemctl enable nftables caddy openobserve" in script
    # The firewall restarts first so a config change can never leave 443 open
    # to the world while caddy comes up.
    assert script.index("systemctl restart nftables") < script.index("systemctl restart openobserve")
    assert script.index("systemctl restart openobserve") < script.index("systemctl restart caddy")


def test_self_collector_pushes_to_loopback_as_the_root_user() -> None:
    collector = self_collector_config(make_instance_config(tier="production"))
    assert collector.role == CollectorRole.INSTANCE
    assert str(collector.ingest_url) == "http://127.0.0.1:5080/"
    # Root basic auth is acceptable here: the same material already sits in
    # openobserve.env on the same disk, and loopback skips the public gate.
    header = collector.ingest_authorization_header_value.get_secret_value()
    assert header.startswith("Basic ")

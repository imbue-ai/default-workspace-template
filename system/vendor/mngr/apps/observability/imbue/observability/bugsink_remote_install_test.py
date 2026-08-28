from imbue.observability.bugsink_remote_install import BUGSINK_REMOTE_ARTIFACT_PATHS
from imbue.observability.bugsink_remote_install import bugsink_cloud_init_path
from imbue.observability.bugsink_remote_install import bugsink_conf_path
from imbue.observability.bugsink_remote_install import bugsink_requirements_path
from imbue.observability.bugsink_remote_install import render_bugsink_install_script
from imbue.observability.bugsink_render import render_all_bugsink_artifacts
from imbue.observability.primitives import BUGSINK_HTTP_PORT
from imbue.observability.testing import make_bugsink_instance_config


def test_every_rendered_artifact_has_a_remote_destination() -> None:
    # The render and the install script must agree on the artifact set, or a
    # deploy would silently skip (or fail on) a config file. The two committed
    # deploy assets are staged alongside the rendered ones.
    artifacts = render_all_bugsink_artifacts(make_bugsink_instance_config(tier="production"))
    assert set(artifacts) | {"bugsink_conf.py", "bugsink_requirements.txt"} == set(BUGSINK_REMOTE_ARTIFACT_PATHS)


def test_the_committed_deploy_assets_exist() -> None:
    assert bugsink_conf_path().is_file()
    assert bugsink_requirements_path().is_file()
    assert bugsink_cloud_init_path().is_file()


def test_requirements_export_is_fully_hash_locked() -> None:
    # Every requirement line must carry sha256 hashes, or the host's
    # --require-hashes install would fail (and an unhashed line would defeat
    # the supply-chain pinning outright).
    requirements_text = bugsink_requirements_path().read_text()
    assert "--hash=sha256:" in requirements_text
    assert "bugsink==2.5.0" in requirements_text


def test_install_script_installs_with_require_hashes_and_stamps_the_digest() -> None:
    script = render_bugsink_install_script("a" * 64)
    assert "pip install --require-hashes --no-deps -r" in script
    # Digest-stamped idempotence: a re-run with unchanged pins skips the pip
    # install; a pin bump re-installs into a fresh venv.
    assert "/usr/local/share/bugsink-requirements.sha256" in script
    assert ("a" * 64) in script
    assert "python3 -m venv --clear /opt/bugsink/venv" in script


def test_install_script_keeps_secret_bearing_files_locked_down() -> None:
    script = render_bugsink_install_script("b" * 64)
    # The env file carries the Django secret key, DSN, and break-glass
    # credentials: readable only by root and the bugsink service user.
    assert 'install -m 0640 -o root -g bugsink "/tmp/bugsink-staging/bugsink.env"' in script
    # The origin TLS material is group-readable by the caddy service user only.
    assert 'install -m 0640 -o root -g caddy "/tmp/bugsink-staging/origin.key"' in script


def test_install_script_runs_bugsink_as_a_dedicated_system_user() -> None:
    script = render_bugsink_install_script("c" * 64)
    assert "useradd --system" in script
    assert "--shell /usr/sbin/nologin bugsink" in script


def test_install_script_waits_out_first_boot_before_installing() -> None:
    # The one-shot recipe deploys right after the VPS reports ACTIVE, but the
    # steps below need cloud-init's packages (caddy and its group, nftables,
    # python3-venv).
    script = render_bugsink_install_script("d" * 64)
    assert "cloud-init status --wait" in script


def test_install_script_enables_and_restarts_all_services() -> None:
    script = render_bugsink_install_script("e" * 64)
    assert "systemctl enable nftables caddy bugsink" in script
    # The firewall restarts first so a config change can never leave 443 open
    # to the world while caddy comes up.
    assert script.index("systemctl restart nftables") < script.index("systemctl restart bugsink")
    assert script.index("systemctl restart bugsink") < script.index("systemctl restart caddy")


def test_cloud_init_unit_matches_the_python_side_port_and_paths() -> None:
    # The systemd unit is a static cloud-init asset while the caddy upstream
    # and readiness probe use the BUGSINK_HTTP_PORT constant; they must agree
    # or the gate would proxy to a port nothing listens on.
    cloud_init_text = bugsink_cloud_init_path().read_text()
    assert f"--bind=127.0.0.1:{BUGSINK_HTTP_PORT}" in cloud_init_text
    assert "EnvironmentFile=/etc/bugsink/bugsink.env" in cloud_init_text
    assert "WorkingDirectory=/opt/bugsink" in cloud_init_text
    # One gunicorn worker, always: Bugsink is a single-writer design.
    assert "--workers=1" in cloud_init_text

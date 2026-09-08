import pytest
from pydantic import AnyHttpUrl
from pydantic import SecretStr

from imbue.observability.collector_install import CollectorConfigRenderError
from imbue.observability.collector_install import render_collector_config
from imbue.observability.collector_install import render_collector_install_script
from imbue.observability.data_types import CollectorInstallConfig
from imbue.observability.primitives import CollectorRole
from imbue.observability.primitives import ObservabilityTierName


def _config(role: CollectorRole, credential: str = "Basic dGVzdDp0ZXN0") -> CollectorInstallConfig:
    return CollectorInstallConfig(
        role=role,
        tier=ObservabilityTierName("staging"),
        ingest_url=AnyHttpUrl("https://telemetry.minds-test.example"),
        ingest_authorization_header_value=SecretStr(credential),
    )


def test_collector_config_exports_to_the_default_org_with_the_sender_credential() -> None:
    rendered = render_collector_config(_config(CollectorRole.BOX))
    assert 'endpoint: "https://telemetry.minds-test.example/api/default"' in rendered
    assert 'Authorization: "Basic dGVzdDp0ZXN0"' in rendered


def test_collector_config_escapes_quotes_and_backslashes_in_the_credential() -> None:
    # A quote or backslash in the (Vault-supplied) credential must round-trip
    # through the YAML double-quoted scalar instead of breaking -- or silently
    # corrupting -- the config the collector parses on the remote host.
    rendered = render_collector_config(_config(CollectorRole.BOX, credential='Digest realm="o2",\\x'))
    assert 'Authorization: "Digest realm=\\"o2\\",\\\\x"' in rendered


def test_collector_config_rejects_a_credential_with_a_control_character() -> None:
    # YAML line folding would corrupt an embedded newline silently; fail
    # loudly at render time instead of at collector startup on the host.
    with pytest.raises(CollectorConfigRenderError, match="control character"):
        render_collector_config(_config(CollectorRole.BOX, credential="Basic dGVzdA==\n"))


def test_collector_config_ships_journal_lines_into_the_role_log_stream() -> None:
    rendered = render_collector_config(_config(CollectorRole.RELAY))
    assert 'stream-name: "relay_logs"' in rendered


def test_collector_config_stamps_tier_and_role_resource_attributes() -> None:
    rendered = render_collector_config(_config(CollectorRole.BOX))
    assert 'value: "staging"' in rendered
    assert 'value: "box"' in rendered


def test_collector_config_watches_qemu_processes_on_boxes() -> None:
    # Box-level qemu process metrics ARE the per-slice visibility signal (an
    # in-guest collector is explicitly forbidden by the spec).
    rendered = render_collector_config(_config(CollectorRole.BOX))
    assert '"^qemu.*"' in rendered
    assert '"^frps$"' not in rendered


def test_collector_config_watches_frps_on_relays_and_openobserve_on_the_instance() -> None:
    assert '"^frps$"' in render_collector_config(_config(CollectorRole.RELAY))
    assert '"^openobserve$"' in render_collector_config(_config(CollectorRole.INSTANCE))


def test_collector_config_has_a_memory_cap_and_a_file_backed_queue() -> None:
    rendered = render_collector_config(_config(CollectorRole.BOX))
    assert "memory_limiter" in rendered
    assert "limit_mib: 256" in rendered
    # The file-backed queue is what buffers through an instance replacement
    # instead of dropping.
    assert "storage: file_storage" in rendered
    assert "directory: /var/lib/otelcol/queue" in rendered


def test_install_script_pins_and_verifies_the_collector_package() -> None:
    script = render_collector_install_script(_config(CollectorRole.BOX))
    assert "otelcol-contrib_0.159.0_linux_${otelcol_goarch}.deb" in script
    assert "sha256sum -c -" in script
    assert "4ede8d750d6bf845e353be46cc550f590e6ccdaeeb60aae941cde6ad561877db" in script
    assert "430469fbfb48f123d08dfc896973bdc205ba393901cc506e92c9c928698a6d5e" in script


def test_install_script_waits_out_first_boot_on_fresh_hosts() -> None:
    # Fresh relay/instance VPSes get this script while first boot may still be
    # installing packages; the wait keeps dpkg/curl from racing cloud-init.
    script = render_collector_install_script(_config(CollectorRole.RELAY))
    assert "cloud-init status --wait" in script


def test_install_script_is_self_contained_and_idempotent() -> None:
    script = render_collector_install_script(_config(CollectorRole.BOX))
    # The rendered config rides inside as a heredoc so box prep, relays, and
    # the instance deploy all run the exact same single artifact.
    assert "OTELCOL_CONFIG_EOF" in script
    assert 'stream-name: "box_logs"' in script
    # Version-gated download: a re-run on a converged host downloads nothing.
    assert "dpkg-query -W" in script
    assert "systemctl restart otelcol-contrib" in script


def test_install_script_grants_journal_access_and_locks_down_the_config() -> None:
    script = render_collector_install_script(_config(CollectorRole.BOX))
    assert "usermod -aG systemd-journal otelcol-contrib" in script
    # The config embeds the ingest credential; owner-only for the service user.
    assert "install -m 0600 -o otelcol-contrib -g otelcol-contrib" in script

import socket
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.observability.cli import OpenObserveNotReadyError
from imbue.observability.cli import SshTunnelExitedError
from imbue.observability.cli import _find_free_local_port
from imbue.observability.cli import _probe_openobserve_ready
from imbue.observability.cli import _wait_for_local_port
from imbue.observability.cli import main


def test_render_collector_install_writes_a_locked_down_script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_OBSERVABILITY_CREDENTIAL_1", "Basic dGVzdDp0ZXN0")
    out_path = tmp_path / "install_collector.sh"

    result = CliRunner().invoke(
        main,
        [
            "render-collector-install",
            "--role",
            "box",
            "--tier",
            "dev",
            "--ingest-url",
            "https://telemetry.minds-test.example",
            "--credential-env-var",
            "TEST_OBSERVABILITY_CREDENTIAL_1",
            "--out",
            str(out_path),
        ],
    )

    assert result.exit_code == 0, result.output
    script = out_path.read_text()
    assert "otelcol-contrib" in script
    assert "Basic dGVzdDp0ZXN0" in script
    # The embedded config carries the ingest credential.
    assert (out_path.stat().st_mode & 0o777) == 0o600


def test_render_collector_install_refuses_a_missing_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TEST_OBSERVABILITY_CREDENTIAL_2", raising=False)

    result = CliRunner().invoke(
        main,
        [
            "render-collector-install",
            "--role",
            "relay",
            "--tier",
            "dev",
            "--ingest-url",
            "https://telemetry.minds-test.example",
            "--credential-env-var",
            "TEST_OBSERVABILITY_CREDENTIAL_2",
            "--out",
            str(tmp_path / "never_written.sh"),
        ],
    )

    # An empty credential must never render a collector that silently fails
    # auth on every push.
    assert result.exit_code != 0
    assert not (tmp_path / "never_written.sh").exists()


def test_deploy_refuses_when_secrets_are_not_in_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OBSERVABILITY_ROOT_EMAIL", raising=False)

    result = CliRunner().invoke(
        main,
        [
            "deploy",
            "--host",
            "203.0.113.7",
            "--tier",
            "dev",
            "--telemetry-hostname",
            "telemetry.minds-test.example",
        ],
    )

    assert result.exit_code != 0
    assert isinstance(result.exception, Exception)
    assert "OBSERVABILITY_ROOT_EMAIL" in str(result.exception)


def test_find_free_local_port_returns_a_bindable_port() -> None:
    port = _find_free_local_port()
    assert 0 < port < 65536
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", port))


def test_wait_for_local_port_returns_once_the_port_accepts() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = int(listener.getsockname()[1])
        with ConcurrencyGroup(name="test-tunnel-alive") as concurrency_group:
            tunnel_process = concurrency_group.run_process_in_background(["true"], is_checked_by_group=False)
            # The port accepts, so the tunnel process is never consulted and
            # the probe returns on the first try.
            _wait_for_local_port(port, tunnel_process)


def test_wait_for_local_port_fails_fast_when_the_tunnel_died() -> None:
    # A dead tunnel (e.g. ssh auth failure under BatchMode) must surface its
    # own error immediately instead of spinning out the full 60s ready window.
    port = _find_free_local_port()
    with ConcurrencyGroup(name="test-tunnel-dead") as concurrency_group:
        tunnel_process = concurrency_group.run_process_in_background(
            ["bash", "-c", "echo 'Permission denied (publickey)' >&2; exit 255"],
            is_checked_by_group=False,
        )
        tunnel_process.wait()
        with pytest.raises(SshTunnelExitedError, match="exited with code 255") as raised:
            _wait_for_local_port(port, tunnel_process)
    assert "Permission denied (publickey)" in str(raised.value)


def test_probe_openobserve_ready_accepts_a_healthy_api() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/healthz"
        return httpx.Response(200, json={"status": "ok"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        _probe_openobserve_ready(client, "http://127.0.0.1:5080")


def test_probe_openobserve_ready_raises_while_the_api_is_still_starting() -> None:
    # Right after a deploy, openobserve may still be migrating its metadata
    # store: an error status must read as "not ready yet" so the wait loop
    # keeps probing instead of the first minting call failing spuriously.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="starting")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(OpenObserveNotReadyError, match="healthz answered 503"):
            _probe_openobserve_ready(client, "http://127.0.0.1:5080")


def test_probe_openobserve_ready_raises_when_nothing_answers_through_the_tunnel() -> None:
    # ssh binds the -L port as soon as it authenticates, so a probe can reach
    # a tunnel whose remote end is refused; that too is "not ready yet".
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(OpenObserveNotReadyError, match="not answering yet"):
            _probe_openobserve_ready(client, "http://127.0.0.1:5080")

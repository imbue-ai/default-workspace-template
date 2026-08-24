"""Synthetic #427: a key path with no known_hosts sibling, driven end to end.

Not a test -- a manual-verification script, kept next to the plan whose
acceptance criteria it satisfies. It is here rather than in the suite because it
spans four layers the suite deliberately tests apart, and because rendering the
card shells out to the frontend toolchain, which CI does not have wired up for
Python tests.

What it drives, in one pass, against real components rather than doubles:

1. the real ``mngr forward`` FastAPI app, against a resolver holding SSH info
   whose key has no known_hosts anywhere near it -- exactly issue #427's shape;
2. the envelope it emits, through minds' real consumer parsing;
3. minds' real ``BackendFailureRecorder`` policy and health tracker;
4. the real ``/ui/api/workspaces/<id>/recovery-info`` route over Flask;
5. the real ``RecoveryCardBody`` component, rendered from that route's payload;
6. and the clear-on-recovery edge, from both the verdict and the route.

Run it with ``uv run python blueprint/decompose-connect-error/verify_427_repro.py``
from the repo root. It needs ``pnpm install`` to have been run in
``apps/minds/frontend`` for step 6 -- that directory is pnpm-managed, and running
``npm install`` in it writes back the npm lockfile the repo ignores.
"""

import io
import json
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from imbue.minds.desktop_client.forward_cli import _parse_backend_failure_reason
from imbue.minds.desktop_client.system_interface_health import BackendFailureRecorder
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.desktop_client.testing import build_resolver_with_system_services
from imbue.minds.desktop_client.ui_api_lifecycle_test import _build_lifecycle_client
from imbue.minds.desktop_client.workspace_recovery import read_device_cannot_connect_verdict
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentInstanceKey
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import HostState
from imbue.mngr_forward.auth import FileAuthStore
from imbue.mngr_forward.data_types import ForwardServiceStrategy
from imbue.mngr_forward.envelope import EnvelopeWriter
from imbue.mngr_forward.primitives import MNGR_FORWARD_SESSION_COOKIE_NAME
from imbue.mngr_forward.resolver import ForwardResolver
from imbue.mngr_forward.server import create_forward_app
from imbue.mngr_forward.ssh_tunnel import RemoteSSHInfo
from imbue.mngr_forward.ssh_tunnel import SSHTunnelManager


def _render_recovery_card(payload: dict) -> str:
    """Render the real RecoveryCardBody against this payload, using the app's own vitest harness."""
    frontend = Path("apps/minds/frontend")
    script = frontend / "repro-427-card.mjs"
    script.write_text(
        "import { RecoveryCardBody } from './src/views/recovery/RecoveryCard.ts';\n"
        "import { RecoveryModel } from './src/models/backups.ts';\n"
        "import { renderRoot, renderedText } from './src/testing.ts';\n"
        "globalThis.window = { mindsNative: { retry: () => {} } };\n"
        f"const info = {json.dumps(payload)};\n"
        "const deps = { getJson: async () => null, postJson: async () => ({ status: 500, json: null }),\n"
        "  deleteResource: async () => 500, openEventSource: () => ({ close: () => {}, onmessage: null, onerror: null }),\n"
        "  schedule: () => {}, redraw: () => {} };\n"
        "const model = new RecoveryModel(info.agent_id, deps);\n"
        "model.info = info;\n"
        "console.log(renderedText(renderRoot(RecoveryCardBody, { model })));\n"
    )
    try:
        result = subprocess.run(
            ["npx", "vite-node", "repro-427-card.mjs"],
            cwd=frontend,
            capture_output=True,
            text=True,
            timeout=180,
        )
    finally:
        script.unlink(missing_ok=True)
    if result.returncode != 0:
        raise AssertionError(f"card render failed: {result.stderr[-2000:]}")
    return result.stdout


def main() -> None:
    """Drive the synthetic #427 reproduction end to end, printing each step."""
    with TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        # A key with NO known_hosts beside it and none supplied: issue #427's shape.
        key_path = tmp_path / "id_ed25519"
        key_path.write_text("not-a-real-key")

        host_id = HostId("host-" + "0" * 31 + "1")
        agent_id = AgentId("agent-" + "0" * 31 + "1")
        instance_key = AgentInstanceKey.build(agent_id, host_id)

        resolver = ForwardResolver(strategy=ForwardServiceStrategy(service_name="system_interface"))
        resolver.add_known_agent(instance_key)
        resolver.update_services(instance_key, {"system_interface": "http://stub-backend:8000"})
        resolver.update_ssh_info(
            instance_key, RemoteSSHInfo(user="root", host="stub-host", port=22, key_path=key_path)
        )

        envelope_output = io.StringIO()
        preauth = "preauth-repro-427"
        app = create_forward_app(
            auth_store=FileAuthStore(data_directory=tmp_path),
            resolver=resolver,
            tunnel_manager=SSHTunnelManager(),
            envelope_writer=EnvelopeWriter(output=envelope_output),
            listen_host="127.0.0.1",
            listen_port=18499,
            preauth_cookie_value=preauth,
        )

        with TestClient(app, base_url=f"http://{host_id}.localhost:18499", follow_redirects=False) as client:
            response = client.get(
                "/",
                headers={
                    "cookie": f"{MNGR_FORWARD_SESSION_COOKIE_NAME}={preauth}",
                    "accept": "text/html,application/xhtml+xml",
                },
            )
        print(f"1. forward served HTTP {response.status_code} (styled loader: {'Loading workspace' in response.text})")

        lines = [line for line in envelope_output.getvalue().splitlines() if line.strip()]
        envelopes = [json.loads(line) for line in lines]
        failures = [e for e in envelopes if e["payload"].get("type") == "system_interface_backend_failure"]
        assert failures, f"no failure envelope; got {envelopes}"
        payload = failures[-1]["payload"]
        print(f"2. envelope reason = {payload['reason']}")
        print(f"   envelope detail = {payload['detail']}")

        # Feed it through minds' real consumer policy.
        tracker = SystemInterfaceHealthTracker()
        BackendFailureRecorder(tracker=tracker)(
            AgentId(payload["agent_id"]),
            _parse_backend_failure_reason(payload["reason"]),
            payload.get("status_code"),
            payload.get("detail"),
        )
        observation = tracker.get_connection_failure(agent_id)
        print(f"3. tracker recorded cause = {observation.reason.value if observation else None}")
        print(f"   agent enrolled as a probe suspect = {agent_id in tracker.snapshot_probe_targets()}")

        verdict = read_device_cannot_connect_verdict(agent_id, tracker=tracker)
        print(f"4. device-cannot-connect verdict = {verdict is not None}")
        print(f"   detail carried to the card = {verdict.detail if verdict else None}")

        # 5. The recovery-info route the card actually polls, over the real Flask app.
        recovery_dir = tmp_path / "recovery"
        recovery_dir.mkdir()
        resolver_for_minds = build_resolver_with_system_services(agent_id, AgentId(), host_state=HostState.RUNNING)
        tracker.mark_stuck(agent_id)
        ui_client, _store = _build_lifecycle_client(recovery_dir, backend_resolver=resolver_for_minds, tracker=tracker)
        route_payload = json.loads(ui_client.get(f"/ui/api/workspaces/{agent_id}/recovery-info").data)
        print(f"5. GET /ui/api/workspaces/<id>/recovery-info -> health={route_payload['health']!r}")
        print(f"   is_device_cannot_connect = {route_payload['is_device_cannot_connect']}")
        print(f"   device_error_detail = {route_payload['device_error_detail'][:60]}...")
        print(f"   is_backend_unreachable = {route_payload['is_backend_unreachable']}")

        # 6. What the card renders from exactly that payload, via the real component.
        card_text = _render_recovery_card(route_payload)
        print("6. card text:")
        for line in card_text.splitlines():
            if line.strip():
                print(f"     {line.strip()}")
        assert "Restart Machine" not in card_text, "the card must not offer to bounce a machine that is probably fine"

        tracker.record_probe_success(agent_id)
        print(
            f"7. verdict after the machine answers = {read_device_cannot_connect_verdict(agent_id, tracker=tracker)}"
        )
        cleared = json.loads(ui_client.get(f"/ui/api/workspaces/{agent_id}/recovery-info").data)
        print(f"   route after the machine answers: is_device_cannot_connect = {cleared['is_device_cannot_connect']}")


if __name__ == "__main__":
    main()

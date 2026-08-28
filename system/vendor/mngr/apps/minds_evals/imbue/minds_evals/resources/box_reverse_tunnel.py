"""Expose a box-local port inside the nested workspace, for the trial's LLM proxy.

Runs inside the box, which has the monorepo's venv (paramiko and mngr_forward are importable). The
direction matters: a Modal sandbox cannot open a tunnel to itself at run time, so the only way into
the box is the SSH channel mngr already holds open to the workspace, used in reverse -- the same
mechanism minds uses in production to reach the desktop gateway from inside a workspace.

Holding the tunnel here rather than in the workspace is what puts the proxy outside the container the
graded agent controls: the agent sees a loopback port and cannot read the upstream key, stop the
meter, or edit its configuration.

With ``--serve-probe-token`` it also answers on that port itself, so the round trip can be checked
before a proxy is listening. Every value it needs is passed in, so it shells out to nothing.
"""

import argparse
import http.server
import threading
from pathlib import Path

from imbue.mngr_forward.ssh_tunnel import RemoteSSHInfo
from imbue.mngr_forward.ssh_tunnel import SSHTunnelManager

PROBE_TOKEN = "minds-evals-reverse-tunnel-ok"


class _TokenHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = PROBE_TOKEN.encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        """Silence the default access log: this process's stdout is the report the caller reads."""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--ssh-user", required=True)
    parser.add_argument("--ssh-host", required=True)
    parser.add_argument("--ssh-port", type=int, required=True)
    parser.add_argument("--ssh-key", required=True)
    parser.add_argument("--port", type=int, default=4000)
    parser.add_argument(
        "--hold-seconds",
        type=float,
        default=180.0,
        help="How long to hold the tunnel open before tearing it down and exiting.",
    )
    parser.add_argument(
        "--serve-probe-token",
        action="store_true",
        help="Answer on the port with a known token, to check the round trip with no proxy running.",
    )
    args = parser.parse_args()

    server = None
    if args.serve_probe_token:
        server = http.server.HTTPServer(("127.0.0.1", args.port), _TokenHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
    manager = SSHTunnelManager()
    try:
        bound_port = manager.setup_reverse_tunnel(
            ssh_info=RemoteSSHInfo(
                user=args.ssh_user, host=args.ssh_host, port=args.ssh_port, key_path=Path(args.ssh_key)
            ),
            local_port=args.port,
            remote_port=args.port,
            agent_id=args.agent_id,
        )
        # The caller waits for this line before using the port from inside the workspace.
        print("TUNNEL_READY port={}".format(bound_port), flush=True)
        # A bounded wait rather than a sleep: the tunnel only needs to outlive the trial, and waiting
        # on an event leaves room to be signalled to stop early. The bound also means a driver that
        # dies without tearing this down cannot leave it running indefinitely.
        threading.Event().wait(args.hold_seconds)
    finally:
        manager.cleanup()
        if server is not None:
            server.shutdown()


if __name__ == "__main__":
    main()

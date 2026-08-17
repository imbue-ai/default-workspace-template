"""Find the Minds backend's API port from INSIDE the box (ported from the old harness's
minds_client.discover_api_port). The desktop app's backend picks a random free port at boot, so we
probe every listening port for /api/v1/workspaces until one answers like Minds. Prints the port on
stdout and exits 0; exits 1 if the deadline passes first.

Runs in the box (Linux, stdlib only): invoked by the driver as
``cd /work/mngr && uv run python /usr/local/bin/probe_minds_port.py [timeout_seconds]``.
"""

import http.client
import json
import sys
import time
import urllib.error
import urllib.request


def _listening_ports() -> list[int]:
    ports: set[int] = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(table) as handle:
                lines = handle.read().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            # fields[1] = local "ADDR:PORT" in hex; fields[3] = state (0A = LISTEN)
            if len(fields) > 3 and fields[3] == "0A":
                try:
                    ports.add(int(fields[1].rsplit(":", 1)[1], 16))
                except (ValueError, IndexError):
                    continue
    return sorted(ports)


def main() -> int:
    timeout_seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 300.0
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        for port in _listening_ports():
            try:
                with urllib.request.urlopen("http://127.0.0.1:{}/api/v1/workspaces".format(port), timeout=2) as resp:
                    data = json.loads(resp.read().decode())
            except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
                # Not HTTP (x11vnc/websockify), not Minds, or not up yet -- keep probing.
                continue
            if isinstance(data, dict) and "workspaces" in data:
                print(port)
                return 0
        time.sleep(3)
    print("could not find the Minds API inside the box (is the Minds app still booting?)", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())

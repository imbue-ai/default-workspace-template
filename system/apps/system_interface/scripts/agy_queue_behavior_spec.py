"""Live behavior spec for agy queuing -- run INSIDE a workspace with a running agy agent.

    uv run python system/apps/system_interface/scripts/agy_queue_behavior_spec.py <agent-name>

Exercises the send-sourced outbox end to end against the REAL backend + agy, sampling the
exact surface the frontend consumes (``/api/agents``: ``queued_messages`` + ``activity_state``)
at 200ms in a background thread. The sampler starts BEFORE any scenario acts, so debounce
windows and drain latencies are observed rather than raced. Each scenario asserts over the
recorded timeline, not point-in-time reads.

Scenarios:
  S1 idle send    -- a message to an idle agy drains within seconds and NEVER surfaces as a
                     queued bubble (the 2s debounce swallows the transient).
  S2 busy queue   -- messages sent while agy is busy surface as bubbles, persist while busy,
                     and ALL clear when the coalesced turn drains (or the idle sweep runs).
  S3 restart      -- with bubbles showing, restart system_interface: after reconnect the
                     bubbles are restored from the agy_outbox ledger, then clear as normal.

The busy phase drives agy with a real long-running instruction, so this is a live test of
the whole loop: send endpoint write-ahead enqueue -> ledger -> agy drain -> verbatim
front-run leave -> WS snapshot. Exit code 0 iff every scenario passes.
"""

import json
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
SAMPLE_INTERVAL_SECONDS = 0.2

# The instruction that keeps agy busy long enough to park messages behind it.
BUSY_PROMPT = "Run this exact shell command with your terminal tool and tell me the output: sleep 25 && echo BUSY_DONE"


def _get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=10) as response:
        return json.loads(response.read())


def _post(path: str, body: dict) -> int:
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code


class Sampler:
    """Polls the frontend's agent state every 200ms into a timestamped timeline."""

    def __init__(self, agent_name: str) -> None:
        self.agent_name = agent_name
        self.samples: list[tuple[float, str, list[str]]] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                agents = _get("/api/agents")["agents"]
                mine = next((a for a in agents if a["name"] == self.agent_name), None)
                if mine is not None:
                    queued = [entry["content"] for entry in (mine.get("queued_messages") or [])]
                    # activity_state is absent until the watcher exists; treat as unknown.
                    self.samples.append((time.monotonic(), mine.get("activity_state") or "?", queued))
            except Exception:
                # Backend down mid-restart (S3) -- keep sampling; gaps are expected.
                pass
            time.sleep(SAMPLE_INTERVAL_SECONDS)

    def since(self, t0: float) -> list[tuple[float, str, list[str]]]:
        return [s for s in self.samples if s[0] >= t0]

    def wait_for(self, predicate, timeout: float, t0: float | None = None) -> bool:
        """True once any sample after ``t0`` satisfies ``predicate``; polls the timeline."""
        deadline = time.monotonic() + timeout
        start = t0 if t0 is not None else 0.0
        while time.monotonic() < deadline:
            if any(predicate(state, queued) for _, state, queued in self.since(start)):
                return True
            time.sleep(0.2)
        return False


def _agent_id(name: str) -> str:
    agents = _get("/api/agents")["agents"]
    matching = [a for a in agents if a["name"] == name]
    if not matching:
        sys.exit(f"agent '{name}' not found")
    return matching[0]["id"]


def _send(agent_id: str, message: str) -> None:
    status = _post(f"/api/agents/{agent_id}/message", {"message": message})
    assert status == 200, f"send failed with {status}"


def _wait_idle(sampler: Sampler, timeout: float = 90) -> None:
    """Block until the CURRENT (tail-of-timeline) state is idle with an empty queue."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        tail = sampler.samples[-1] if sampler.samples else None
        if tail and tail[1] == "IDLE" and not tail[2]:
            return
        time.sleep(0.5)
    sys.exit(f"agent never settled idle within {timeout}s (last: {sampler.samples[-1:]})")


def scenario_1_idle_send(sampler: Sampler, agent_id: str) -> str:
    _wait_idle(sampler)
    t0 = time.monotonic()
    _send(agent_id, "S1: reply with exactly OK and nothing else")
    # Drain: agent leaves IDLE (or answers fast); give it a generous window.
    time.sleep(8)
    surfaced = [q for _, _, q in sampler.since(t0) if any("S1:" in c for c in q)]
    if surfaced:
        return f"FAIL: idle send surfaced as a queued bubble: {surfaced[:3]}"
    return "PASS"


def scenario_2_busy_queue(sampler: Sampler, agent_id: str) -> str:
    _wait_idle(sampler)
    _send(agent_id, BUSY_PROMPT)
    # Wait until agy is demonstrably busy (out of IDLE) before parking messages.
    t_busy = time.monotonic()
    if not sampler.wait_for(lambda s, q: s != "IDLE", 30, t0=t_busy):
        return "FAIL: agent never went busy on the sleep prompt"
    time.sleep(2)
    t0 = time.monotonic()
    _send(agent_id, "S2 first parked message")
    time.sleep(1)
    _send(agent_id, "S2 second parked message")
    # Bubbles must surface (post-debounce) while the turn runs.
    if not sampler.wait_for(lambda s, q: len([c for c in q if c.startswith("S2")]) == 2, 15, t0=t0):
        return f"FAIL: parked messages never surfaced as bubbles (tail: {sampler.samples[-5:]})"
    # And must ALL clear once the busy turn ends and the coalesced drain (or sweep) lands.
    if not sampler.wait_for(lambda s, q: not [c for c in q if c.startswith("S2")], 120, t0=t0):
        return f"FAIL: bubbles never cleared (tail: {sampler.samples[-5:]})"
    return "PASS"


def scenario_3_restart_persistence(sampler: Sampler, agent_id: str) -> str:
    _wait_idle(sampler)
    _send(agent_id, BUSY_PROMPT)
    t_busy = time.monotonic()
    if not sampler.wait_for(lambda s, q: s != "IDLE", 30, t0=t_busy):
        return "FAIL: agent never went busy"
    time.sleep(2)
    t0 = time.monotonic()
    _send(agent_id, "S3 parked across restart")
    if not sampler.wait_for(lambda s, q: any(c.startswith("S3") for c in q), 15, t0=t0):
        return "FAIL: bubble never surfaced before restart"
    subprocess.run(["supervisorctl", "restart", "system_interface"], check=True, capture_output=True)
    t_restart = time.monotonic()
    # Re-prime the watcher once the API is back (the real frontend reconnects and
    # reopens the transcript, which is what triggers the ledger replay).
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            _get(f"/api/agents/{agent_id}/events?limit=1")
            break
        except Exception:
            time.sleep(0.5)
    # Reconnect + watcher rebuild + ledger replay: the bubble must come back...
    if not sampler.wait_for(lambda s, q: any(c.startswith("S3") for c in q), 45, t0=t_restart):
        return f"FAIL: bubble not restored from ledger after restart (tail: {sampler.samples[-5:]})"
    # ...and still clear once the turn drains or the idle sweep runs.
    if not sampler.wait_for(lambda s, q: not any(c.startswith("S3") for c in q), 120, t0=t_restart):
        return f"FAIL: restored bubble never cleared (tail: {sampler.samples[-5:]})"
    return "PASS"


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    agent_name = sys.argv[1]
    agent_id = _agent_id(agent_name)
    # Prime the watcher BEFORE sampling: the queue surface only flows once the agent's
    # session watcher exists (the real frontend does this by opening the transcript).
    _get(f"/api/agents/{agent_id}/events?limit=1")
    sampler = Sampler(agent_name)
    sampler.start()
    time.sleep(1)

    results = {}
    for scenario in (scenario_1_idle_send, scenario_2_busy_queue, scenario_3_restart_persistence):
        name = scenario.__name__
        print(f"--- {name} ...", flush=True)
        try:
            results[name] = scenario(sampler, agent_id)
        except Exception as error:
            results[name] = f"ERROR: {error!r}"
        print(f"    {results[name]}", flush=True)

    sampler.stop()
    print(json.dumps(results, indent=2))
    if any(v != "PASS" for v in results.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()

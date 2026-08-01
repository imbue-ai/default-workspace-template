"""Live terminal dashboard for the pixelflux stream telemetry (Rung 1).

A watch-only CLI companion to the ``/telemetry`` firehose: it connects to the same
read-only WebSocket, joins nothing on the server, and redraws a compact dashboard in
place every ~0.4s. Run it in a terminal you keep open while you drive the browser.

    uv run python -m browser.telemetry_watch [browser-name]

With no name it auto-picks the (single) running browser, or lists them if there are
several. It reaches the daemon directly on 127.0.0.1:8081, so no proxy is involved.

Everything shown is derived in this process from the raw records; it never influences
the stream. The round-trip RTT includes the viewer's decode+paint (Rung 1 has no
client taps yet), and the TCP block is the local hop only -- both labeled as such.
"""

import asyncio
import collections
import json
import sys
import urllib.request

import websockets

_DAEMON = "127.0.0.1:8081"
_WINDOW_S = 30.0
_UNACK_TIMEOUT_S = 2.0
_C = {  # ANSI colors
    "dim": "\033[2m", "b": "\033[1m", "r": "\033[31m", "g": "\033[32m",
    "y": "\033[33m", "c": "\033[36m", "gray": "\033[90m", "x": "\033[0m",
}


def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    return values[min(len(values) - 1, int(p / 100 * len(values)))]


def _pick_browser(explicit: str | None) -> str:
    if explicit:
        return explicit
    with urllib.request.urlopen(f"http://{_DAEMON}/browsers", timeout=5) as response:
        data = json.load(response)
    names = [b["id"] for b in data.get("browsers", [])]
    if not names:
        sys.exit("No browsers in the fleet. Open one first, then re-run.")
    if len(names) > 1:
        sys.exit("Several browsers running; pass one:\n  " + "\n  ".join(names))
    return names[0]


class _State:
    """Rolling telemetry state, pruned to the last _WINDOW_S of records."""

    def __init__(self) -> None:
        self.acks: collections.deque = collections.deque()   # (t, y, rtt_ms)
        self.dwell: collections.deque = collections.deque()  # (t, ms)
        self.rate: dict | None = None
        self.tcp: dict | None = None
        self.pending: dict = {}                              # key -> t_sent
        self.drops = {"mailbox-overwrite": 0, "sticky-idr": 0, "needs-keyframe": 0}
        self.sent = self.ack = self.idr = self.unacked = 0
        self.max_t = 0.0
        self.last_seq: int | None = None
        self.missed = 0

    def ingest(self, r: dict) -> None:
        seq = r.get("seq")
        if seq is not None:
            if self.last_seq is not None and seq > self.last_seq + 1:
                self.missed += seq - self.last_seq - 1
            self.last_seq = seq
        t = r.get("t", self.max_t)
        self.max_t = max(self.max_t, t)
        kind = r.get("type")
        if kind == "sent":
            self.sent += 1
            self.dwell.append((t, max(0.0, (r["t_sent"] - r["t_mailboxed"]) * 1000)))
            self.pending[f'{r["epoch"]}:{r["fid"]}:{r["y"]}'] = r["t_sent"]
        elif kind == "ack":
            self.ack += 1
            self.acks.append((t, r["y"], r["rtt"] * 1000))
            self.pending.pop(f'{r["epoch"]}:{r["fid"]}:{r["y"]}', None)
        elif kind == "rate":
            self.rate = r
        elif kind == "drop":
            if r["reason"] in self.drops:
                self.drops[r["reason"]] += 1
        elif kind == "idr":
            self.idr += 1
        elif kind == "tcpinfo":
            self.tcp = r

    def prune(self) -> None:
        cut = self.max_t - _WINDOW_S
        for dq in (self.acks, self.dwell):
            while dq and dq[0][0] < cut:
                dq.popleft()
        stale = self.max_t - _UNACK_TIMEOUT_S
        for k, ts in list(self.pending.items()):
            if ts < stale:
                self.unacked += 1
                del self.pending[k]


def _verdict(floor: float | None, p50: float | None, p99: float | None, n: int) -> tuple[str, str, str]:
    """(color, headline, detail) judging distance vs loss vs queuing.

    Keyed off the FLOOR (the best round trip ~= propagation), not the median: distance
    puts the median right on the floor with a thin tail; a median well above the floor is
    persistent queuing; a tail far above the median is bursty loss / head-of-line stalls.
    (Round trip still includes client decode+paint in Rung 1, so a slow client reads as
    queuing/loss too -- honest, since that also hurts the user.)"""
    if n < 20 or floor is None or p50 is None or p99 is None:
        return _C["gray"], "not enough samples", f"({n}) — drive the browser to generate traffic"
    tail = p99 - p50
    lift = p50 - floor
    if p50 <= floor * 1.6 and p99 <= floor * 2.5:
        return (_C["g"], "DISTANCE",
                f"steady round trip on the floor, thin tail (floor {floor:.0f} · p50 {p50:.0f} · p99 {p99:.0f} ms)")
    if tail > lift and p99 > p50 * 2:
        return (_C["r"], "LOSS / STALLS",
                f"bursty tail p99 {p99:.0f}ms ≫ p50 {p50:.0f}ms over floor {floor:.0f} — packet loss / head-of-line")
    return (_C["y"], "QUEUING",
            f"median {p50:.0f}ms sits {p50 / floor:.1f}x above the {floor:.0f}ms floor — persistent congestion")


def _render(state: _State, browser_id: str, connected: bool, live: bool = True) -> str:
    state.prune()
    rtts = sorted(rtt for _, _, rtt in state.acks)
    p50, p95, p99 = _pct(rtts, 50), _pct(rtts, 95), _pct(rtts, 99)
    floor, mx, n = (rtts[0] if rtts else None), (rtts[-1] if rtts else None), len(rtts)

    def f(v: float | None) -> str:
        return "—" if v is None else (f"{v:.1f}" if v < 10 else f"{v:.0f}")

    def col(v: float | None, warn: float, bad: float) -> str:
        if v is None:
            return _C["gray"]
        return _C["r"] if v >= bad else _C["y"] if v >= warn else _C["g"]

    lines = []
    dot = f'{_C["g"]}●{_C["x"]}' if connected else f'{_C["r"]}●{_C["x"]}'
    miss = f'   {_C["y"]}⚠ missed {state.missed} records{_C["x"]}' if state.missed else ""
    lines.append(f'{dot} {_C["b"]}stream telemetry{_C["x"]}  {_C["c"]}{browser_id}{_C["x"]}{miss}')
    lines.append(_C["gray"] + "─" * 66 + _C["x"])

    # verdict (floor-relative; see _verdict)
    vc, vhead, vdetail = _verdict(floor, p50, p99, n)
    lines.append(f'{vc}{_C["b"]}verdict: {vhead}{_C["x"]} {vc}— {vdetail}{_C["x"]}')

    # round trip
    lines.append("")
    lines.append(f'{_C["b"]}round trip{_C["x"]} {_C["gray"]}(transport + client render; ms){_C["x"]}')
    sc = _C["g"] if n >= 20 else _C["y"]
    lines.append(
        f'  p50 {col(p50, 60, 120)}{f(p50):>5}{_C["x"]}   p95 {col(p95, 100, 200)}{f(p95):>5}{_C["x"]}   '
        f'p99 {col(p99, 150, 300)}{f(p99):>5}{_C["x"]}   max {f(mx):>5}   floor {f(floor):>5}   '
        f'{sc}n={n}/{int(_WINDOW_S)}s{_C["x"]}'
    )

    # cross-row
    rows: dict = {}
    for _, y, rtt in state.acks:
        rows.setdefault(y, []).append(rtt)
    if rows:
        parts = []
        for y in sorted(rows):
            vals = sorted(rows[y])
            parts.append(f'y={y}: p50 {f(_pct(vals, 50))} / p99 {f(_pct(vals, 99))} (n={len(vals)})')
        lines.append(f'  {_C["gray"]}per row:{_C["x"]} ' + "   ".join(parts))
        lines.append(f'  {_C["gray"]}(all rows spike together = network/HOL · one row alone = that decoder){_C["x"]}')

    # capture rate
    lines.append("")
    if state.rate:
        rr = state.rate
        ew = rr.get("rtt_ewma")
        lines.append(
            f'{_C["b"]}capture{_C["x"]}   applied {_C["c"]}{rr["applied_fps"]:.0f}{_C["x"]} fps'
            f'   ceiling {rr["ceiling"]:.0f}   AIMD {rr["aimd_fps"]:.0f}   CRF {rr["crf"]}'
            f'   window {rr["limit"]}   {_C["gray"]}reason {rr["reason"]}{_C["x"]}'
        )
        if rr["ceiling"] <= 3:
            lines.append(f'  {_C["y"]}↳ throttled to ~{rr["ceiling"]:.0f}fps: the pane is hidden, not the network{_C["x"]}')
    else:
        lines.append(f'{_C["b"]}capture{_C["x"]}   {_C["gray"]}waiting for a rate decision…{_C["x"]}')

    # mailbox dwell
    dvals = sorted(ms for _, ms in state.dwell)
    lines.append(
        f'{_C["b"]}mailbox{_C["x"]}   wait-for-credit  p50 {f(_pct(dvals, 50))}   '
        f'p95 {f(_pct(dvals, 95))}   p99 {f(_pct(dvals, 99))} ms   {_C["gray"]}(server-side; ms){_C["x"]}'
    )

    # mortality
    d = state.drops
    lines.append("")
    ua = _C["r"] if state.unacked else _C["g"]
    lines.append(
        f'{_C["b"]}drops{_C["x"]}     overwrite {d["mailbox-overwrite"]}   sticky-idr {d["sticky-idr"]}   '
        f'need-key {d["needs-keyframe"]}   keyframes {state.idr}   '
        f'{ua}unacked {state.unacked}{_C["x"]}   {_C["gray"]}sent/ack {state.sent}/{state.ack}{_C["x"]}'
    )

    # tcp local hop
    if state.tcp:
        t = state.tcp
        rc = _C["y"] if t["total_retrans"] else _C["g"]
        lines.append(
            f'{_C["b"]}local hop{_C["x"]} rtt {t["rtt_us"] / 1000:.2f}ms   {rc}retrans {t["total_retrans"]}{_C["x"]}   '
            f'cwnd {t["snd_cwnd"]}   unacked {t["unacked"]}   {_C["gray"]}(loopback only — not the WAN){_C["x"]}'
        )
    else:
        lines.append(f'{_C["b"]}local hop{_C["x"]} {_C["gray"]}no TCP sample yet (needs an active viewer streaming){_C["x"]}')

    lines.append("")
    if live:
        lines.append(f'{_C["gray"]}Ctrl-C to quit · refreshes ~2.5x/s · a viewer must be streaming for live numbers{_C["x"]}')
    else:
        lines.append(f'{_C["gray"]}point-in-time snapshot · a viewer must be streaming for live numbers{_C["x"]}')
    return "\n".join(lines)


async def _sample(browser_id: str, duration: float) -> None:
    """One-shot: collect telemetry for ``duration`` seconds, print a single snapshot,
    exit. This is ``--sample`` -- a point-in-time reading, not the live loop."""
    state = _State()
    url = f"ws://{_DAEMON}/browsers/{browser_id}/telemetry"
    connected = False
    try:
        async with websockets.connect(url, max_size=None) as ws:
            connected = True
            end = asyncio.get_event_loop().time() + duration
            while asyncio.get_event_loop().time() < end:
                remaining = end - asyncio.get_event_loop().time()
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                batch = json.loads(msg)
                for rec in (batch if isinstance(batch, list) else [batch]):
                    state.ingest(rec)
    except Exception as error:  # noqa: BLE001
        sys.exit(f"could not read telemetry for {browser_id}: {error}")
    sys.stdout.write(_render(state, browser_id, connected, live=False) + "\n")


async def _watch(browser_id: str) -> None:
    state = _State()
    url = f"ws://{_DAEMON}/browsers/{browser_id}/telemetry"
    connected = False

    async def reader() -> None:
        nonlocal connected
        while True:
            try:
                async with websockets.connect(url, max_size=None) as ws:
                    connected = True
                    async for msg in ws:
                        batch = json.loads(msg)
                        for rec in (batch if isinstance(batch, list) else [batch]):
                            state.ingest(rec)
            except Exception:  # noqa: BLE001  (reconnect on any drop)
                connected = False
                await asyncio.sleep(1.0)

    async def painter() -> None:
        while True:
            sys.stdout.write("\033[2J\033[H" + _render(state, browser_id, connected) + "\n")
            sys.stdout.flush()
            await asyncio.sleep(0.4)

    await asyncio.gather(reader(), painter())


def main() -> None:
    # Tiny hand-rolled arg parse (kept dependency-free): [browser-name] [--sample] [--for N].
    args = sys.argv[1:]
    sample = "--sample" in args
    duration = 3.0
    if "--for" in args:
        i = args.index("--for")
        try:
            duration = float(args[i + 1])
        except (IndexError, ValueError):
            sys.exit("--for needs a number of seconds, e.g. --for 5")
        del args[i : i + 2]
    positional = [a for a in args if not a.startswith("--")]
    browser_id = _pick_browser(positional[0] if positional else None)
    try:
        if sample:
            asyncio.run(_sample(browser_id, duration))
        else:
            asyncio.run(_watch(browser_id))
    except KeyboardInterrupt:
        sys.stdout.write("\033[0m\n")


if __name__ == "__main__":
    main()

"""Live terminal dashboard for the pixelflux stream telemetry (watch-only).

    uv run python -m browser.telemetry_watch [browser-name] [--sample] [--for N]

Connects to the daemon's read-only ``/telemetry`` firehose (directly on
127.0.0.1:8081), derives everything in-process, and never influences the stream.
No name auto-picks the single running browser. ``--sample`` prints one snapshot and
exits; otherwise it redraws in place ~2.5x/s.

Two things worth knowing to read the output honestly: pure network = server round
trip minus the client's own decode+paint (both same-machine durations, so the
subtraction needs no clock sync); and the TCP block is the daemon's local hop only
(it peers with a local forwarder, so it can't see WAN loss).

Depends only on the stdlib and ``websockets`` -- no imports from the browser package.
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
_C = {
    "b": "\033[1m", "r": "\033[31m", "g": "\033[32m",
    "y": "\033[33m", "c": "\033[36m", "gray": "\033[90m", "x": "\033[0m",
}


def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    return values[min(len(values) - 1, int(p / 100 * len(values)))]


def _stats(dq: "collections.deque") -> dict:
    vals = sorted(v for _, v in dq)
    return {
        "p50": _pct(vals, 50), "p95": _pct(vals, 95), "p99": _pct(vals, 99),
        "floor": vals[0] if vals else None, "max": vals[-1] if vals else None, "n": len(vals),
    }


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
    """Rolling telemetry, pruned to the last _WINDOW_S. Deques hold (t, value...) tuples."""

    def __init__(self) -> None:
        self.acks: collections.deque = collections.deque()   # (t, y, rtt_ms)
        self.dwell: collections.deque = collections.deque()  # (t, mailbox_ms)
        self.rate: dict | None = None
        self.tcp: dict | None = None
        self.resource: dict | None = None
        self.pending: dict = {}                              # "epoch:fid:y" -> t_sent
        self.drops = {"mailbox-overwrite": 0, "sticky-idr": 0, "needs-keyframe": 0}
        self.sent = self.ack = self.idr = self.unacked = 0
        self.max_t = 0.0
        self.last_seq: int | None = None
        self.missed = 0
        self._ack_rtt: dict = {}                             # (fid,y) -> (rtt_ms, t)
        self._client_pending: dict = {}                      # (fid,y) -> client record
        self.pure_net: collections.deque = collections.deque()     # (t, ms)
        self.client_hold: collections.deque = collections.deque()  # (t, arrive->paint ms)
        self.decode: collections.deque = collections.deque()       # (t, arrive->decoded ms)
        self.dq_max = 0
        self.client_err = 0

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
            rtt_ms = r["rtt"] * 1000
            self.acks.append((t, r["y"], rtt_ms))
            self.pending.pop(f'{r["epoch"]}:{r["fid"]}:{r["y"]}', None)
            key = (r["fid"], r["y"])
            self._ack_rtt[key] = (rtt_ms, t)
            client = self._client_pending.pop(key, None)
            if client is not None:
                self._join(t, rtt_ms, client)
        elif kind == "client":
            if r.get("err"):
                self.client_err += 1
            elif "fid" in r:
                key = (r["fid"], r["y"])
                self.dq_max = max(self.dq_max, r.get("dq", 0))
                pair = self._ack_rtt.get(key)
                if pair is not None:
                    self._join(t, pair[0], r)
                else:
                    self._client_pending[key] = r
        elif kind == "rate":
            self.rate = r
        elif kind == "resource":
            self.resource = r
        elif kind == "drop":
            if r["reason"] in self.drops:
                self.drops[r["reason"]] += 1
        elif kind == "idr":
            self.idr += 1
        elif kind == "tcpinfo":
            self.tcp = r

    def _join(self, t: float, rtt_ms: float, client: dict) -> None:
        # pure network = server RTT - client hold; both are same-machine durations, so the
        # subtraction is offset-free (no clock sync between server and viewer).
        hold = client["t_painted"] - client["t_arrived"]
        decode = client["t_decoded"] - client["t_arrived"]
        self.client_hold.append((t, max(0.0, hold)))
        self.decode.append((t, max(0.0, decode)))
        self.pure_net.append((t, max(0.0, rtt_ms - hold)))

    def prune(self) -> None:
        cut = self.max_t - _WINDOW_S
        for dq in (self.acks, self.dwell, self.pure_net, self.client_hold, self.decode):
            while dq and dq[0][0] < cut:
                dq.popleft()
        stale = self.max_t - _UNACK_TIMEOUT_S
        for k, ts in list(self.pending.items()):
            if ts < stale:
                self.unacked += 1
                del self.pending[k]
        for store in (self._ack_rtt, self._client_pending):
            for k in [k for k, v in store.items() if (v[1] if isinstance(v, tuple) else v.get("t", self.max_t)) < cut]:
                store.pop(k, None)


def _verdict(floor: float | None, p50: float | None, p99: float | None, n: int) -> tuple[str, str, str]:
    # Keyed off the FLOOR (best round trip ~= propagation): distance keeps the median on
    # the floor with a thin tail; a lifted median is queuing; a tail far above the median
    # is bursty loss / head-of-line.
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


def _bottleneck(state: "_State", pnet: dict, hold: dict, have_pure: bool, net_head: str) -> str:
    hot = []
    rs = state.resource
    if rs is not None:
        # Judge compute off the browser's own vCPU spend (per-process; reliable under gVisor,
        # which zeroes host-wide cpu), against the box ceiling of ncpu*100%.
        browser_cpu = rs["daemon_cpu"] + rs["chrome_cpu"]
        ceiling = rs["ncpu"] * 100
        if browser_cpu >= ceiling * 0.85:
            hot.append(f'{_C["r"]}COMPUTE{_C["x"]} (browser {browser_cpu:.0f}% of {ceiling}% on {rs["ncpu"]} vCPU)')
        if rs["mem_pct"] >= 92 or rs["swap_pct"] >= 10:
            hot.append(f'{_C["r"]}MEMORY{_C["x"]} (mem {rs["mem_pct"]:.0f}%, swap {rs["swap_pct"]:.0f}%)')
    if have_pure and hold["p95"] is not None and hold["p95"] >= 30:
        hot.append(f'{_C["y"]}RENDERER{_C["x"]} (client hold p95 {hold["p95"]:.0f}ms)')
    # Derived from the SAME verdict shown above so the two lines can never disagree.
    if have_pure and net_head in ("LOSS / STALLS", "QUEUING"):
        hot.append(f'{_C["r"]}NETWORK{_C["x"]} (pure p50 {pnet["p50"]:.0f} / p99 {pnet["p99"]:.0f} over floor {pnet["floor"]:.0f}ms)')
    if not hot:
        if not have_pure:
            return f'{_C["gray"]}need the real viewer + traffic to attribute (compute/mem shown below){_C["x"]}'
        return f'{_C["g"]}none dominant — network floor is distance, client fast, cpu/mem healthy{_C["x"]}'
    return "  ·  ".join(hot)


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

    pnet = _stats(state.pure_net)
    hold = _stats(state.client_hold)
    dec = _stats(state.decode)
    have_pure = pnet["n"] >= 15
    if have_pure:
        vc, vhead, vdetail = _verdict(pnet["floor"], pnet["p50"], pnet["p99"], pnet["n"])
        lines.append(f'{vc}{_C["b"]}verdict (pure network): {vhead}{_C["x"]} {vc}— {vdetail}{_C["x"]}')
    else:
        vc, vhead, vdetail = _verdict(floor, p50, p99, n)
        lines.append(f'{vc}{_C["b"]}verdict (round trip): {vhead}{_C["x"]} {vc}— {vdetail}{_C["x"]}')
        lines.append(f'  {_C["gray"]}(no client render data yet — this includes decode+paint; open the real viewer for pure network){_C["x"]}')
    lines.append(f'{_C["b"]}bottleneck:{_C["x"]} ' + _bottleneck(state, pnet, hold, have_pure, vhead))

    lines.append("")
    lines.append(f'{_C["b"]}round trip{_C["x"]} {_C["gray"]}(transport + client render; ms){_C["x"]}')
    sc = _C["g"] if n >= 20 else _C["y"]
    lines.append(
        f'  p50 {col(p50, 60, 120)}{f(p50):>5}{_C["x"]}   p95 {col(p95, 100, 200)}{f(p95):>5}{_C["x"]}   '
        f'p99 {col(p99, 150, 300)}{f(p99):>5}{_C["x"]}   max {f(mx):>5}   floor {f(floor):>5}   '
        f'{sc}n={n}/{int(_WINDOW_S)}s{_C["x"]}'
    )

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

    lines.append("")
    if have_pure:
        lines.append(f'{_C["b"]}pure network{_C["x"]} {_C["gray"]}(round trip − client render; ms){_C["x"]}')
        lines.append(
            f'  p50 {col(pnet["p50"], 60, 120)}{f(pnet["p50"]):>5}{_C["x"]}   p95 {f(pnet["p95"]):>5}   '
            f'p99 {col(pnet["p99"], 150, 300)}{f(pnet["p99"]):>5}{_C["x"]}   floor {f(pnet["floor"]):>5}   {_C["g"]}n={pnet["n"]}{_C["x"]}'
        )
        lines.append(
            f'{_C["b"]}client render{_C["x"]} decode {f(dec["p50"])}/{f(dec["p95"])}   '
            f'hold(arrive→paint) p50 {f(hold["p50"])} / p95 {col(hold["p95"], 25, 60)}{f(hold["p95"])}{_C["x"]} ms   '
            f'dq_max {state.dq_max}   {(_C["r"] + "errors " + str(state.client_err) + _C["x"]) if state.client_err else "errors 0"}'
        )
    else:
        lines.append(f'{_C["b"]}pure network{_C["x"]} {_C["gray"]}waiting for the real viewer to report decode/paint{_C["x"]}')

    lines.append("")
    if state.rate:
        rr = state.rate
        lines.append(
            f'{_C["b"]}capture{_C["x"]}   applied {_C["c"]}{rr["applied_fps"]:.0f}{_C["x"]} fps'
            f'   ceiling {rr["ceiling"]:.0f}   AIMD {rr["aimd_fps"]:.0f}   CRF {rr["crf"]}'
            f'   window {rr["limit"]}   {_C["gray"]}reason {rr["reason"]}{_C["x"]}'
        )
        if rr["ceiling"] <= 3:
            lines.append(f'  {_C["y"]}↳ throttled to ~{rr["ceiling"]:.0f}fps: the pane is hidden, not the network{_C["x"]}')
    else:
        lines.append(f'{_C["b"]}capture{_C["x"]}   {_C["gray"]}waiting for a rate decision…{_C["x"]}')

    dvals = sorted(ms for _, ms in state.dwell)
    lines.append(
        f'{_C["b"]}mailbox{_C["x"]}   wait-for-credit  p50 {f(_pct(dvals, 50))}   '
        f'p95 {f(_pct(dvals, 95))}   p99 {f(_pct(dvals, 99))} ms   {_C["gray"]}(server-side; ms){_C["x"]}'
    )

    d = state.drops
    lines.append("")
    ua = _C["r"] if state.unacked else _C["g"]
    lines.append(
        f'{_C["b"]}drops{_C["x"]}     overwrite {d["mailbox-overwrite"]}   sticky-idr {d["sticky-idr"]}   '
        f'need-key {d["needs-keyframe"]}   keyframes {state.idr}   '
        f'{ua}unacked {state.unacked}{_C["x"]}   {_C["gray"]}sent/ack {state.sent}/{state.ack}{_C["x"]}'
    )

    if state.tcp:
        t = state.tcp
        rc = _C["y"] if t["total_retrans"] else _C["g"]
        lines.append(
            f'{_C["b"]}local hop{_C["x"]} rtt {t["rtt_us"] / 1000:.2f}ms   {rc}retrans {t["total_retrans"]}{_C["x"]}   '
            f'cwnd {t["snd_cwnd"]}   unacked {t["unacked"]}   {_C["gray"]}(loopback only — not the WAN){_C["x"]}'
        )
    else:
        lines.append(f'{_C["b"]}local hop{_C["x"]} {_C["gray"]}no TCP sample yet (needs an active viewer streaming){_C["x"]}')

    if state.resource:
        rs = state.resource
        ncpu = rs["ncpu"]
        browser_cpu = rs["daemon_cpu"] + rs["chrome_cpu"]
        pct_of_box = browser_cpu / (ncpu * 100) * 100
        lines.append("")
        lines.append(
            f'{_C["b"]}cpu{_C["x"]}   browser {col(pct_of_box, 70, 90)}{pct_of_box:.0f}%{_C["x"]} of {ncpu} vCPU'
            f'   {_C["gray"]}= encoder{_C["x"]} {rs["daemon_cpu"]:.0f}%{_C["gray"]} + chromium{_C["x"]} {rs["chrome_cpu"]:.0f}%'
            f' {_C["gray"]}({rs["chrome_procs"]} procs; 100% = one core){_C["x"]}'
        )
        # gVisor doesn't expose host-wide cpu; show it only when the daemon flagged it usable.
        if rs.get("sys_ok", True):
            lines.append(f'      {_C["gray"]}host sys cpu {rs["sys_cpu"]:.0f}%   load {rs["load1"]}{_C["x"]}')
        else:
            lines.append(f'      {_C["gray"]}host sys cpu n/a (sandbox doesn\'t report it) — using per-process above{_C["x"]}')
        lines.append(
            f'{_C["b"]}mem{_C["x"]}   used {col(rs["mem_pct"], 85, 95)}{rs["mem_pct"]:.0f}%{_C["x"]}   '
            f'avail {rs["mem_avail_mb"]}MB   swap {col(rs["swap_pct"], 5, 25)}{rs["swap_pct"]:.0f}%{_C["x"]}   '
            f'{_C["gray"]}enc {rs["daemon_rss_mb"]}MB · chrome {rs["chrome_rss_mb"]}MB{_C["x"]}'
        )

    lines.append("")
    if live:
        lines.append(f'{_C["gray"]}Ctrl-C to quit · refreshes ~2.5x/s · a viewer must be streaming for live numbers{_C["x"]}')
    else:
        lines.append(f'{_C["gray"]}point-in-time snapshot · a viewer must be streaming for live numbers{_C["x"]}')
    return "\n".join(lines)


async def _sample(browser_id: str, duration: float) -> None:
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

"""Manual-verification driver for the transcript smooth-scroll engine.

Drives the standalone fixture server's chat with real wheel events and
scrollbar drags, asserting the spec's must-pass scenarios with
content-relative measurements (row rects + the ?debug=scroll trace), not
scrollTop deltas.
"""

import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE_URL = os.environ.get("SCROLL_VERIFY_URL", "http://127.0.0.1:8642")
# Required: the fixture session JSONL the server tails (streaming scenarios append to it).
SESSION_FILE = Path(sys.argv[1])

GET_STATE = """
(() => {
  const el = document.querySelector('.transcript-scroll');
  if (!el) return null;
  const list = el.querySelector('.message-list');
  if (!list) return null;
  const spacers = Array.from(list.children).filter(c => c.id === '').map(s => parseFloat(s.style.height));
  const rows = Array.from(list.children).filter(c => c.id !== '');
  const elRect = el.getBoundingClientRect();
  let topRow = null;
  for (const r of rows) {
    const rect = r.getBoundingClientRect();
    if (rect.bottom > elRect.top + 1) { topRow = { id: r.id, top: rect.top - elRect.top }; break; }
  }
  const t = window.__scrollTrace ? window.__scrollTrace.dump() : [];
  const kinds = {};
  for (const e of t) kinds[e.kind] = (kinds[e.kind] || 0) + 1;
  const transitions = t.filter(e => e.kind === 'transition').map(e => e.detail.from + '->' + e.detail.to + ':' + e.detail.event);
  return {
    scrollTop: el.scrollTop, scrollHeight: el.scrollHeight, clientHeight: el.clientHeight,
    spacers, mounted: rows.length, topRow, kinds, transitions,
    bottomGap: el.scrollHeight - el.scrollTop - el.clientHeight,
  };
})()
"""


def row_top(page, row_key):
    return page.evaluate(
        """(key) => {
      const el = document.querySelector('.transcript-scroll');
      const row = document.getElementById(key);
      if (!el || !row) return null;
      return row.getBoundingClientRect().top - el.getBoundingClientRect().top;
    }""",
        row_key,
    )


RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(
        f"{'PASS' if ok else 'FAIL'}: {name}{('  [' + str(detail) + ']') if detail else ''}",
        flush=True,
    )


def make_stream_line(i):
    return json.dumps(
        {
            "type": "assistant",
            "uuid": f"stream-{uuid.uuid4()}",
            "timestamp": f"2026-08-25T12:00:{i:02d}.000Z",
            "message": {
                "role": "assistant",
                "model": "claude-fable-5",
                "content": [
                    {
                        "type": "text",
                        "text": f"Streaming filler message {i} "
                        + ("lorem ipsum " * 12),
                    }
                ],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        }
    )


def stream_appender(count, interval_s):
    with SESSION_FILE.open("a") as f:
        for i in range(count):
            f.write(make_stream_line(i) + "\n")
            f.flush()
            time.sleep(interval_s)


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1200, "height": 800})
        page.goto(f"{BASE_URL}/?debug=scroll")
        page.wait_for_selector(".transcript-scroll", timeout=30000)
        page.evaluate("localStorage.clear()")
        # Paint-synced FOLLOW-gap sampler: records the bottom gap once per frame
        # (after the engine's afterRender corrections), which is what is painted.
        page.evaluate("""(() => {
          // A single-frame gap can slip in when content resizes outside any
          // redraw (e.g. an image load); the engine's list ResizeObserver pins
          // on the very next frame. Sustained gaps are the real failure.
          window.__maxSustainedGap = 0;
          let run = 0;
          const sample = () => {
            const el = document.querySelector('.transcript-scroll');
            if (el && window.__scrollDebugState && window.__scrollDebugState().positionKind === 'FOLLOW') {
              const gap = el.scrollHeight - el.scrollTop - el.clientHeight;
              run = gap > 60 ? run + 1 : 0;
              if (run >= 2 && gap > window.__maxSustainedGap) window.__maxSustainedGap = gap;
            } else {
              run = 0;
            }
            requestAnimationFrame(sample);
          };
          requestAnimationFrame(sample);
        })()""")

        # --- A: initial load pins the tail; progressive fill loads the whole chat ---
        deadline = time.time() + 90
        state = None
        debug = None
        while time.time() < deadline:
            state = page.evaluate(GET_STATE)
            debug = page.evaluate(
                "window.__scrollDebugState ? window.__scrollDebugState() : null"
            )
            if state is None or debug is None:
                time.sleep(0.5)
                continue

            if (
                debug["extent"]["firstIndex"] == 0
                and debug["spacerTopPx"] == 0
                and debug["spacerBottomPx"] == 0
            ):
                break
            time.sleep(1.0)
        filled = (
            debug
            and debug["extent"]["firstIndex"] == 0
            and debug["spacerTopPx"] == 0
            and debug["spacerBottomPx"] == 0
        )
        check(
            "A1 whole transcript filled into physical (virtual spacers -> 0)",
            filled,
            debug,
        )
        max_sustained_gap = page.evaluate("window.__maxSustainedGap")
        check(
            "A2 FOLLOW stayed pinned to the bottom during fill (no sustained painted gap)",
            max_sustained_gap <= 60,
            f"maxSustainedGap={max_sustained_gap}",
        )
        check(
            "A3 no user-position transitions during fill",
            state and all("MESSAGE" not in t for t in state["transitions"]),
            state and state["transitions"],
        )

        # --- B0: one tiny wheel tick must leave the tail and stay left ---
        # Direction decides intent: a few-px first gesture event must not be
        # re-pinned to the bottom by the positional band.
        page.mouse.move(600, 400)
        page.mouse.wheel(0, -24)
        page.wait_for_timeout(700)
        state = page.evaluate(GET_STATE)
        dbg = page.evaluate("window.__scrollDebugState()")
        check(
            "B0 a small wheel tick leaves FOLLOW and is not yanked back",
            dbg["positionKind"] == "USER_CONTROLLED" and state["bottomGap"] >= 5,
            (dbg["positionKind"], round(state["bottomGap"])),
        )
        page.mouse.wheel(0, 200)
        page.wait_for_timeout(300)

        # --- A4: wheel-up immediately, while fills and measurements still land ---
        # The reported live bug: the row spanning the viewport top gets its real
        # measurement (estimate -> actual) mid-scroll; anchoring the NEXT row let
        # the top message reflow. Anchor the spanning row and it cannot move.
        page.goto(f"{BASE_URL}/?debug=scroll")
        page.wait_for_selector(".transcript-scroll", timeout=30000)
        page.wait_for_timeout(400)
        page.mouse.move(600, 400)
        # Per-frame flash sampler: a mounting tall row must not shift the top
        # message even for a single painted frame (rAF granularity, which the
        # polling loop below cannot see).
        page.evaluate("""(() => {
          window.__frameFlashes = [];
          let prev = null;
          const sample = () => {
            const el = document.querySelector('.transcript-scroll');
            if (el) {
              const list = el.querySelector('.message-list');
              const elRect = el.getBoundingClientRect();
              let top = null;
              for (const r of Array.from(list.children).filter(c => c.id !== '')) {
                const rect = r.getBoundingClientRect();
                if (rect.bottom > elRect.top + 1) { top = { id: r.id, top: rect.top - elRect.top }; break; }
              }
              if (top && prev && prev.id === top.id && top.top < prev.top - 6) {
                window.__frameFlashes.push([prev.top, top.top, top.id.slice(0, 18)]);
              }
              prev = top;
            }
            requestAnimationFrame(sample);
          };
          requestAnimationFrame(sample);
        })()""")
        early_violations = []
        witness = None
        last_top = None
        for step in range(25):
            page.mouse.wheel(0, -260)
            page.wait_for_timeout(70)
            state = page.evaluate(GET_STATE)
            top_row = state["topRow"] if state else None
            if top_row is None:
                continue
            if witness == top_row["id"] and last_top is not None and top_row["top"] < last_top - 5:
                early_violations.append((step, last_top, top_row["top"]))
            witness = top_row["id"]
            last_top = top_row["top"]
        frame_flashes = page.evaluate("window.__frameFlashes")
        check(
            "A4 no jumps wheel-scrolling up while unmeasured history loads",
            len(early_violations) == 0,
            early_violations[:3],
        )
        check(
            "A5 no single-frame flashes as tall rows mount during the scroll",
            len(frame_flashes) == 0,
            frame_flashes[:3],
        )

        # Fresh FOLLOW start for phase B: the A4 reload restores the persisted
        # USER_CONTROLLED position, so clear it and reload once more. The engine
        # debounces persistence by 300ms, so a clear issued right after scrolling
        # gets overwritten when the pending persist fires; let it fire first.
        page.wait_for_timeout(600)
        page.evaluate("localStorage.clear()")
        page.goto(f"{BASE_URL}/?debug=scroll")
        page.wait_for_selector(".transcript-scroll", timeout=30000)
        page.wait_for_timeout(1200)

        # --- B: wheel-up during streaming: disengage + no downward jumps ---
        appender = threading.Thread(
            target=stream_appender, args=(15, 0.25), daemon=True
        )
        appender.start()
        page.mouse.move(600, 400)
        page.mouse.wheel(0, -300)
        page.wait_for_timeout(150)
        state = page.evaluate(GET_STATE)
        check(
            "B1 wheel-up enters USER_CONTROLLED",
            any(t.startswith("FOLLOW->USER_CONTROLLED") for t in state["transitions"]),
            state["transitions"][-3:],
        )
        witness = state["topRow"]["id"] if state["topRow"] else None
        check("B2 an anchor row is on screen", witness is not None)
        violations = []
        last_top = row_top(page, witness)
        for step in range(20):
            page.mouse.wheel(0, -240)
            page.wait_for_timeout(80)
            now_top = row_top(page, witness)
            if now_top is None:
                break  # witness scrolled far below and unmounted: pick it up no further
            # Wheeling UP must move the witness DOWN the screen (or hold); a drop
            # UPWARD of more than a few px against wheel direction is a jump.
            if now_top < last_top - 5:
                violations.append((step, last_top, now_top))
            last_top = now_top
        check(
            "B3 no backwards jumps while wheel-scrolling up during streaming",
            len(violations) == 0,
            violations[:3],
        )
        state = page.evaluate(GET_STATE)
        check(
            "B4 still USER_CONTROLLED while streaming appends",
            not state["transitions"]
            or not state["transitions"][-1].endswith("EVENTS_APPENDED"),
            state["transitions"][-2:],
        )
        appender.join()

        # --- B6: sub-pixel gesture starts must not be eaten while streaming ---
        # A trackpad gesture begins with many deltas of ~1px. While the agent
        # streams, content changes every frame, so positioning runs every frame;
        # any threshold that classifies small deltas as "not user input" reverts
        # each one before the next arrives -- the viewport stays pinned to the
        # bottom no matter how long the user scrolls (the reproduced live bug).
        page.evaluate(
            "(() => { const el = document.querySelector('.transcript-scroll'); el.scrollTop = el.scrollHeight; })()"
        )
        page.wait_for_timeout(700)
        appender3 = threading.Thread(target=stream_appender, args=(30, 0.08), daemon=True)
        appender3.start()
        # Frame-rate churn like a live streaming message: grow the last row's
        # text every frame so positioning re-runs continuously.
        page.evaluate("""(() => {
          window.__growTimer = setInterval(() => {
            const el = document.querySelector('.transcript-scroll');
            const rows = Array.from(el.querySelectorAll('.message-list > [id]'));
            const last = rows[rows.length - 1];
            if (last) { last.appendChild(document.createTextNode(' stream')); }
          }, 16);
        })()""")
        page.wait_for_timeout(300)
        for _ in range(30):
            page.mouse.wheel(0, -1)
            page.wait_for_timeout(8)
        page.wait_for_timeout(700)
        state = page.evaluate(GET_STATE)
        dbg = page.evaluate("window.__scrollDebugState()")
        page.evaluate("clearInterval(window.__growTimer)")
        appender3.join()
        check(
            "B6 a stream of 1px wheel-ups leaves the tail while streaming",
            dbg["positionKind"] == "USER_CONTROLLED" and state["bottomGap"] >= 25,
            (dbg["positionKind"], round(state["bottomGap"])),
        )

        # --- C: anchored reading position is rock-solid while fills/streams land ---
        state = page.evaluate(GET_STATE)
        witness = state["topRow"]["id"]
        start_top = row_top(page, witness)
        appender2 = threading.Thread(
            target=stream_appender, args=(10, 0.2), daemon=True
        )
        appender2.start()
        drift = 0.0
        for _ in range(12):
            page.wait_for_timeout(250)
            now = row_top(page, witness)
            if now is not None:
                drift = max(drift, abs(now - start_top))
        appender2.join()
        state = page.evaluate(GET_STATE)
        check(
            "C1 anchored row pixel-stable while events stream in (drift <= 2px)",
            drift <= 2.0,
            f"drift={drift}",
        )
        check(
            "C2 clamp/echo suppression kept machine quiet (no spurious FOLLOW)",
            "USER_CONTROLLED->FOLLOW:USER_SCROLLED" not in state["transitions"][-6:],
            state["transitions"][-4:],
        )

        # --- D: scrollbar drag to the very bottom returns to FOLLOW ---
        el_box = page.evaluate(
            "(() => { const e = document.querySelector('.transcript-scrollbar'); if (!e) return null; const r = e.getBoundingClientRect(); return {x: r.x + r.width / 2, top: r.y, height: r.height}; })()"
        )
        check("D0 custom scrollbar exists", el_box is not None)
        if el_box:
            page.mouse.move(el_box["x"], el_box["top"] + el_box["height"] * 0.5)
            page.mouse.down()
            # Overshoot past the track end: the fraction clamps to 1 = the true bottom.
            page.mouse.move(el_box["x"], el_box["top"] + el_box["height"] + 40, steps=5)
            page.mouse.up()
            # Steady state: stragglers from the streaming scenarios may still be
            # landing; FOLLOW must converge on the pinned bottom.
            for _ in range(12):
                page.wait_for_timeout(250)
                state = page.evaluate(GET_STATE)
                if state["bottomGap"] < 45:
                    break
            check(
                "D1 dragging the thumb to the bottom re-enters FOLLOW",
                state["bottomGap"] < 45
                and any(
                    t == "USER_CONTROLLED->FOLLOW:USER_SCROLLED"
                    for t in state["transitions"]
                ),
                (state["bottomGap"], state["transitions"][-3:]),
            )

            # --- E: scrollbar jump into the top virtual region (if any remains) or physical top ---
            page.mouse.move(el_box["x"], el_box["top"] + 3)
            page.mouse.down()
            page.mouse.up()
            page.wait_for_timeout(1500)
            state = page.evaluate(GET_STATE)
            near_top = state["scrollTop"] < state["scrollHeight"] * 0.05
            check(
                "E1 track click at 0% lands at the start of the conversation",
                near_top,
                state["scrollTop"],
            )
            check(
                "E2 no viewport left in a blank spacer after the jump",
                state["topRow"] is not None and state["topRow"]["top"] < 200,
                state["topRow"],
            )

        # --- E3: anchored mid-history, fills landing above must not move the view ---
        page.mouse.move(600, 400)
        page.mouse.wheel(0, 800)
        page.wait_for_timeout(300)
        state = page.evaluate(GET_STATE)
        witness = state["topRow"]["id"] if state["topRow"] else None
        start_top = row_top(page, witness) if witness else None
        drift3 = 0.0
        for _ in range(16):
            page.wait_for_timeout(250)
            now = row_top(page, witness) if witness else None
            if now is not None and start_top is not None:
                drift3 = max(drift3, abs(now - start_top))
        check(
            "E3 anchored view pixel-stable while history fills in above (drift <= 2px)",
            witness is not None and drift3 <= 2.0,
            f"drift={drift3}",
        )

        # --- E4: expanding a block at the top of the screen must not move it ---
        # Reproduces the live bug pair: rows carry CSS margins, so height-based
        # geometry drifted from the DOM cumulatively and deep anchors resolved
        # rows off; expanding a block then wrote a large false compensation.
        expand_checks = []
        for _ in range(30):
            target = page.evaluate("""(() => {
              const el = document.querySelector('.transcript-scroll');
              const elRect = el.getBoundingClientRect();
              for (const t of el.querySelectorAll('.tool-call-header')) {
                const r = t.getBoundingClientRect();
                if (r.top > elRect.top + 4 && r.top < elRect.top + 260 && r.height > 4) {
                  return { x: r.x + 40, y: r.y + r.height / 2 };
                }
              }
              return null;
            })()""")
            if target is None:
                page.mouse.wheel(0, -900)
                page.wait_for_timeout(80)
                continue
            for _toggle in range(2):
                before = page.evaluate(GET_STATE)
                page.mouse.click(target["x"], target["y"])
                page.wait_for_timeout(400)
                after = page.evaluate(GET_STATE)
                if before["topRow"] and after["topRow"]:
                    same = before["topRow"]["id"] == after["topRow"]["id"]
                    delta = abs(after["topRow"]["top"] - before["topRow"]["top"]) if same else 999
                    expand_checks.append((same, round(delta, 2)))
            if len(expand_checks) >= 6:
                break
        check(
            "E4 expanding/collapsing a block at the top keeps the top message put",
            len(expand_checks) >= 2 and all(same and delta <= 4 for same, delta in expand_checks),
            expand_checks,
        )

        # --- F: persistence across reload ---
        page.mouse.wheel(0, 3000)
        page.wait_for_timeout(200)
        page.mouse.wheel(0, -1200)
        page.wait_for_timeout(700)  # persist debounce
        saved = page.evaluate(
            "localStorage.getItem('transcript-scroll:agent-scrollfix-1')"
        )
        check(
            "F1 scroll state persisted to localStorage",
            saved is not None and '"USER_CONTROLLED"' in (saved or ""),
            (saved or "")[:80],
        )
        pre_state = page.evaluate(GET_STATE)
        pre_anchor = pre_state["topRow"]["id"] if pre_state["topRow"] else None
        page.reload()
        page.wait_for_selector(".transcript-scroll", timeout=30000)
        # The restore completes once the fill reaches the persisted location;
        # on a large transcript that can outlast a fixed wait. Poll for the
        # restore trace record.
        state = None
        for _ in range(40):
            page.wait_for_timeout(250)
            state = page.evaluate(GET_STATE)
            if state and state["kinds"].get("restore", 0) >= 1:
                break
        restored = state["kinds"].get("restore", 0) >= 1
        top_after = state["topRow"]["id"] if state["topRow"] else None
        check(
            "F2 reload restores the anchored position",
            restored and top_after == pre_anchor,
            (pre_anchor, top_after, state["transitions"][:3]),
        )

        # --- G: send-message snap (drive the composer for real) ---
        page.click("textarea")
        page.fill("textarea", "verification ping")
        page.keyboard.press("Enter")
        for _ in range(20):
            page.wait_for_timeout(200)
            state = page.evaluate(GET_STATE)
            if any(t.endswith("MESSAGE_SENT") for t in state["transitions"]):
                break
        check(
            "G1 sending a message snaps back to FOLLOW at the bottom",
            any(t.endswith("MESSAGE_SENT") for t in state["transitions"])
            and state["bottomGap"] < 45,
            (state["bottomGap"], state["transitions"][-3:]),
        )

        browser.close()

    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed", flush=True)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()

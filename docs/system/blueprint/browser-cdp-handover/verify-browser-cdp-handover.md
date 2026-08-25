# Verifying the CDP handover

How to convince yourself this change is safe, from the chat side, in about ten minutes.

Everything in the browser feature worked before this change, so this only probes what moved:
**launching Chromium, the CDP path the agent drives over, and the ownership lease**. The pixel
and input layer -- streaming, XTEST input, clipboard, the take-control overlay, window pinning
-- has a **zero-line diff** in this branch. If those misbehave, it is not this change; do not
spend time on them.

The scenarios are ordered so that a failure localises the cause: if 2 fails, 3-6 will too.

---

## 1. Cold start

> "Open a browser and go to news.ycombinator.com, tell me the top story."

**Expect:** the pane opens, the page loads, it answers. No second attempt, and nothing about
needing to acquire the browser first.

**If it cannot drive at all** -- the attach URL `new` prints is not authorised. The agent's
first CDP frame is supposed to take the lease on a resting browser.

## 2. Take the wheel mid-task

> "Go to Wikipedia and search for three different topics, one at a time."

While it works, click **Take control**.

**Expect:** its very next action fails, and it tells you the human took over and stops. No
retrying, no fighting you for the cursor.

**If it keeps clicking while you type** -- the per-frame lease check is not biting. This is the
core product guarantee; treat a failure here as blocking.

## 3. Hand it back

From 2, click **Return control to agents**, then:

> "keep going"

**Expect:** it picks up in the **same** browser. No new browser, no "I lost the session".

**If it says it cannot resume, or opens a fresh browser** -- the agent's socket was killed by
the handover. The token must survive a human takeover; only a *different agent* taking the
browser may invalidate it.

## 4. Two agents, one browser

With agent A mid-task, ask a second agent:

> "Use the browser to look something up."

**Expect:** it is told another agent holds that browser, and it either waits or opens its own
(fleet cap permitting). It must **not** drive A's browser.

**If it hijacks A's tabs** -- agent-vs-agent exclusion. A raw CDP client sends no identity
header, so the capability token in the attach URL is the only thing separating two agents.

## 5. Tab following

> "Open two tabs -- one on example.com, one on wikipedia.org -- then read me the heading on the
> example.com one."

While it works, click onto the *other* tab yourself.

**Expect:** the pane snaps to whichever tab the agent is acting on. When it does something
tab-less (`ls`), you stay where you are.

This is not a heuristic: each CDP frame carries the `sessionId` Chrome itself uses to route the
command, and the proxy fronts exactly that target.

## 6. Restart and resume

Restart the workspace, then reopen the browser.

**Expect:** the tabs come back, **in the same order**, on the tab that was active, and sites you
were logged into are still logged in.

**If tabs come back shuffled** -- `Target.getTargets` returns targets in an arbitrary order and
something is indexing into it rather than tracking creation order.

## 7. Crash

Kill Chromium from a terminal: `pkill -f tilion`

**Expect:** within ~20s the pane shows "This browser crashed". The agent's next command fails;
asked to continue, it should run `ls`, see `crashed`, and start a **new** browser rather than
re-attaching the dead one. The freed slot lets `new` work immediately.

## 8. Close

> "close the browser"

**Expect:** the terminated overlay, the name retired, and a fresh `new` works right after.

---

# The original bug: a virtualized list stuck at 25 items

The report that started this work:

> *Meditation (47) and Personal Growth (33) are stuck at 25 each -- the browser tool can't
> scroll their long virtualized lists, and the autonomous scroller needs an API key this
> workspace doesn't have.*

Two separate faults, both gone:

* **The scroll did nothing.** The old `scroll` verb ran `window.scrollBy()`, which is a no-op on
  any page whose content lives in an inner scroll container -- every virtualized list. Worse, it
  reported `ok`, so the agent believed it had scrolled and gave up on a page it had never moved.
* **The fallback needed a key.** The only other option was `task`, an LLM-driven verb requiring
  an Anthropic API key. That verb is gone; **nothing in the browser needs a key now.**

The fix is that there is no custom scroll verb at all. `playwright-cli mousewheel` dispatches a
real wheel event, which the browser hit-tests to the actual scroll container -- inner pane,
virtualized list, or iframe.

## Reproducing it deterministically

The failure needs a *virtualized* list: one where only the visible rows exist in the DOM, so the
rest cannot be found without really scrolling. Run this in the workspace:

```bash
mkdir -p /tmp/scrolltest && cat > /tmp/scrolltest/virt.html <<'HTML'
<!doctype html><meta charset=utf-8><title>Virtualized folder list</title>
<style>html,body{margin:0;height:100vh;overflow:hidden;font:14px sans-serif}
#list{height:100vh;overflow:auto}#spacer{position:relative}
.row{position:absolute;height:40px;left:0;right:0;border-bottom:1px solid #eee}</style>
<div id=list><div id=spacer></div></div>
<script>
const N=200,H=40; spacer.style.height=(N*H)+'px';
function render(){const t=list.scrollTop,f=Math.floor(t/H),l=Math.min(N,f+Math.ceil(list.clientHeight/H)+1);
 spacer.innerHTML='';for(let i=f;i<l;i++){const d=document.createElement('a');d.className='row';
 d.href='#r'+i;d.style.top=(i*H)+'px';d.textContent='folder item '+i;spacer.appendChild(d);}}
list.addEventListener('scroll',render);render();
</script>
HTML
cd /tmp/scrolltest && python3 -m http.server 8899 --bind 127.0.0.1 &
```

Only ~18 of the 200 rows exist in the DOM at any moment -- the same shape as the folder lists in
the report.

Then ask an agent:

> "Open a browser, go to http://127.0.0.1:8899/virt.html and tell me the name of folder item 150."

**Expect:** it finds it. Under the hood that is roughly

```
playwright-cli -s=browser-1 goto http://127.0.0.1:8899/virt.html
playwright-cli -s=browser-1 find "folder item 150"    # No matches -- not rendered yet
playwright-cli -s=browser-1 mousewheel 0 800          # ~20 rows per wheel
# ...about eight times...
playwright-cli -s=browser-1 find "folder item 150"    # Found
```

Measured on a real browser: the rendered window walks `[0,18] -> [20,38] -> ... -> [120,138]`,
a deterministic 20 rows per 800px wheel. **Under the old code the first `scroll` returned `ok`
and the window never left `[0,18]`.**

Against the real app from the report, the equivalent ask is "list everything in the Meditation
folder" -- expect 47, not 25.

## The one way this still goes wrong

`mousewheel` scrolls at the **last mouse position, which defaults to (0,0)** -- the top-left
corner. On a two-pane layout that is the sidebar, so the agent can scroll the wrong container and
see nothing move. The fix is `hover <ref>` inside the intended pane first, and the
`agentic-browser-fleet` skill says so. If you watch an agent scroll with nothing happening, this is the
first thing to check.

---

# If something fails

`playwright-cli` exits 1 for *every* failure and cannot say why -- a revoked lease and a stale
element ref look identical. So the rule, for you and for the agent, is the same:

```bash
uv run agentic-browser-fleet ls
```

That is the only thing that distinguishes "the human took control" from "the browser crashed"
from "your ref went stale".

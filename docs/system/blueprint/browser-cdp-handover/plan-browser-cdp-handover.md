# Plan: hand the agent CDP, not verbs

> **Remove browser-use entirely. The fleet keeps owning Chromium's lifecycle -- private display, profile, cap, ownership lease -- and hands the agent a gated CDP endpoint instead of our own driving verbs. The agent drives with `@playwright/cli`. Streaming, ownership, and human-takeover are preserved; the guarantee that today lives in `run_action()`'s per-command compare-and-set moves into a per-frame check in the proxy.**
>
> **Honest bound on the guarantee.** Today no agent action can touch a human-held browser. After this change no agent *input event* can, and no agent *frame* is forwarded once the lease flips -- but a frame already handed to Chromium may still land, exactly as one in-flight action can today. `Runtime.evaluate` is unfilterable (§6.1), so the residual is one in-flight JS evaluation rather than one in-flight verb. This is a real, bounded weakening. It is not "no weakening."

## Verified constraints

Everything below was measured against `@playwright/cli` 0.1.18 + Chrome 151 through a working
prototype proxy, unless marked *read*. Corrections to earlier drafts are marked.

**Architecture validated end to end.** `playwright-cli attach --cdp=<proxy>` connects through a
gated CDP proxy, drives the page, and survives lease refusal. The prototype rewrote discovery,
filtered `/json/list`, and toggled allow/refuse/drop at runtime.

| Fact | Detail |
|---|---|
| **Refusing forwarded frames does NOT poison the session** | Refused commands error; flipping back to allow, the **same slug recovered and drove normally**. This is why §4.2 refuses rather than closes. |
| **Dropping the socket poisons the slug permanently** | After one drop the slug never recovered, and `playwright-cli list` no longer showed it at all. Confirms browser-blitz SKILL.md:184. **Earlier drafts proposed exactly this.** |
| A fresh slug re-attaches to the same browser | The recovery path after a poisoned slug. |
| **Discovery is fetched with a trailing slash** | Playwright requests `/json/version/`, not `/json/version`. A proxy routing only the bare path fails with `Unexpected status 404` and the daemon exits 1. Cost me an hour; costs the implementer nothing if written down. |
| **`@playwright/cli close` does NOT kill an attached external browser** | Chrome survived (`Browser 'pt4' closed`, pid alive). **CORRECTION** -- earlier drafts flagged this as an unverified risk. Keep the `Browser.close` block as cheap insurance, not as a critical control. |
| `attach` uses the **default** browser context, not incognito | So `Target.createBrowserContext` is insurance, not load-bearing. |
| Three CDP clients coexist | fleet-raw + second-raw + playwright-cli-through-proxy, all live simultaneously. §3's third client is safe. |
| **`tab-list` already shows only real pages** | Extension `background_page`, `service_worker`, and `chrome://` `browser_ui` targets are filtered by Playwright itself. **CORRECTION** -- earlier drafts required mirroring our filter onto websocket target events; not needed. |
| **The 10-vs-5 target count is explained** | A fresh profile with ONE page reports 5 targets: 2 `browser_ui` (`chrome://omnibox-popup`), 1 extension `background_page`, 1 `service_worker`, 1 real `page`. No mystery. |
| **`playwright-cli` blocks `file:` itself** | `goto file:///etc/hosts` -> `Access to "file:" protocol is blocked`. But `chrome://version` and `http://127.0.0.1` both navigate fine, and `fetch()` from page JS to loopback succeeds. See §6.1. |
| **Snapshot cost is two different things** | The *automatic* snapshot after each command is a **file link** (`.playwright-cli/page-*.yml`). The *explicit* `snapshot` verb prints **inline** -- 48,832 bytes on a 600-link page. |
| **Exit codes are non-zero on error but undifferentiated** | `rc=1` for both a lease refusal and a stale ref. **CORRECTION** -- an earlier measurement of mine said "always 0"; that was a shell error in my test harness (`$?` captured `head`). See §5.3. |
| **Refusal error text is misleading** | A refused frame surfaces as `Execution context was destroyed, most likely because of a navigation.` Our CDP error message does not reach the agent. Some verbs instead hang the full 30s and return `TimeoutError`. |
| **The virtualized-list case works with plain `mousewheel`** | 6 wheels walked a 200-row virtualized list from rendered rows `[0,18]` to `[120,138]` -- deterministic 20 rows per wheel, no hover, no targeting. Resolves §9.1. |
| `connect_over_cdp` imposes no viewport override on adopted pages | `[1200,657,1]` held across attach/drive/detach. |
| **Playwright's own switch list adds `--enable-automation` and `--disable-extensions`** (`chromiumSwitches.js:92`, `:69`); browser-use's `ignore_default_args` exists to strip them (`profile.py:428`) | *read* -- **CORRECTION**, earlier drafts had this backwards |
| `no_viewport`, `keep_alive`, `window_size` are **options, not flags** (`session.py:776-782`); `launch_persistent_context` defaults to a 1280x720 viewport | *read* |
| `[program:browser]` sets `stopasgroup`/`killasgroup` (`supervisord.conf:373`); Chromium does not survive a service restart, and `restore()` always launches fresh | *read* |
| Agent identity is the client-supplied `x-mngr-agent-id` header (`runner.py:213`); a generic CDP client sends nothing | *read* |
| `_LEASE_IDLE_TTL = 60` (`session.py:337`), 10s sweep -- real expiry 60-70s | *read* -- **CORRECTION**, drafts and SKILL.md say 90s |
| **`extract` does not exist** -- no verb, no route, only a string in `anthropic_key_status` (`session.py:539`) | *read* -- **CORRECTION** |

## 1. What is removed

| Removed | Location |
|---|---|
| `act_state` / `act_navigate` / `act_click` / `act_input` / `act_select` / `act_keys` / `act_screenshot` / `act_tab` | `session.py` ~1675-1990 |
| `run_agent`, `Agent`, `ChatAnthropic`, `task`, `anthropic_key_status`, `resolve_anthropic_key` | `session.py` |
| `ActionHandler` / `BrowserSession` imports, `_bu_alive`, `_build_bu_session`, `_ensure_action_handler`, `_selector_map`, `_node` | `session.py:60-61` and callers |
| `cmd_state`/`cmd_open`/`cmd_click`/`cmd_input`/`cmd_select`/`cmd_keys`/`cmd_screenshot`/`cmd_tab`/`cmd_task`, `_render_action`'s per-verb branches | `fleet.py` |
| Routes: the eight drive routes **plus `/browsers/<id>/task` (`runner.py:1071`) and `/key-status` (`runner.py:1066`)** | `runner.py` |
| `key_available` from `GET /browsers` and `POST /browsers`, and its assertion in `system_interface/frontend/src/models/Browsers.test.ts:19` | `runner.py`, frontend |
| `cmd_lock` (`fleet.py:420`) + `POST /browsers/<id>/hold` + `_stream_acquire`/`_make_on_wait` -- **undocumented in SKILL.md; no agent is told it exists** | `fleet.py`, `runner.py:515` |
| The driving half of the skill | `.agents/skills/agentic-browser-fleet/SKILL.md` |
| `browser-use[core]==0.13.1`, `cdp-use`, the `openai`/`litellm` pin override | `system/apps/browser/pyproject.toml`, root `pyproject.toml` |

**Traded, not free:** browser-use's DOM serializer (the curated numbered `state` listing) is replaced by playwright-cli's ref'd snapshots. Snapshots are **inline by default** -- the vendored skill must mandate `--filename=` or `find`, or the context cost is worse than today.

**`scroll` is deliberately NOT in the removal list** -- see §9.1.

**Do not rename the profile path.** `_profile_dir` uses `browser-use-user-data-dir-<name>` (`session.py:452`) to defeat browser-use's `_copy_profile()`. That reason dies with the package; renaming strands every persisted login. Keep the path byte-identical, retire only the comment and the test that guards the rationale.

## 2. Launch: `subprocess.Popen`, with browser-use's argument list ported as data

**Do not use `launch_persistent_context`.** It applies Playwright's default switches, which include `--enable-automation` (sets `navigator.webdriver = true` on a stealth-patched fork whose purpose is not doing that -- `session.py:761` rejects it by name) and `--disable-extensions` plus `--disable-component-extensions-with-background-pages` (kills the §6.3 vendored extensions on Chrome 145+). It also imposes a 1280x720 viewport, and it gives Python no process handle and no way to read the debug port (`cdpPort` is not exposed in the Python API; the pipe-transport branch discards the parsed endpoint).

- **Port `CHROME_DEFAULT_ARGS` and the `ignore_default_args` strip-list from browser-use into our tree as data** (~40 lines) before deleting the package. That list is a curated fork of Playwright's with the anti-stealth switches removed; it is the actual value browser-use was providing at launch.
- **Capture the effective command line from a running browser** (`/proc/<pid>/cmdline`) and pin that set, so nothing is lost silently. Our own twelve flags are a subset of what runs today.
- **Carry the non-flag options explicitly**: `no_viewport=True` (page fills the real OS window -- the 1:1 capture mapping), `keep_alive=True`, `window_size`. These are launch semantics, not flags, and a flag-list migration will miss them.
- Keep `--test-type` (its rationale survives `--load-extension=`: it disables only *component* extensions, `session.py:763`) and `--window-position=0,0`.
- Keep the sandbox-off retry (`session.py:785-805`) -- a real runtime accommodation.
- `--remote-debugging-port=0`, then poll `DevToolsActivePort` in the profile dir. **Add `DevToolsActivePort` to `_SINGLETON_LOCK_NAMES` (`session.py:449`)** or a relaunch reads the previous run's port (§8.2).
- Bind loopback only.

## 3. The fleet's own CDP client

With browser-use gone the service loses its browser-scoped CDP channel. This is load-bearing and was missing from earlier drafts. A single persistent raw CDP client per browser, owned by `LiveBrowser`, serves:

- `_tab_list` (`session.py:974`), `tab_urls` (`session.py:988`, every ~10s), `_active_target` / `_active_url`
- `_focus_and_foreground` (`session.py:951`, `Target.activateTarget`) and `_open_initial_tabs` (`session.py:912`, `Target.createTarget`) -- both browser-level, unavailable from a Playwright persistent context
- **Crash detection** (§5.1)

It owns the reconnect-vs-dead classification that `_bu_alive` provided for free. Note this makes three CDP clients on one browser (fleet, proxy-forwarded agent, and any human tooling); only two were measured as coexisting -- verify three in phase 0.

## 4. The proxy

New `cdp_proxy.py`. **Its own loopback port, NOT mounted on the Flask app**: `runner.py:1151` binds `127.0.0.1:8081` but `supervisord.conf:367` registers that URL with `forward_port.py`, which publishes it in `data/.state/apps.toml` for the desktop client -- mounting CDP there would expose it through that forward. Do not register the proxy port.

### 4.1 Capability token, not a bare browser name

The proxy cannot read `x-mngr-agent-id` from a generic CDP client, so it cannot tell one agent from another. Without a token, agent B attaches to agent A's browser and the proxy allows it -- the fleet's core exclusion, gone.

- URL is `http://127.0.0.1:<port>/<name>/<token>`.
- Minted by `new` / `acquire`, printed in the `drive it:` line, **rotated on every ownership write** (`_write_control_locked`, `session.py:1094`) and **on every Chromium launch**.
- Lease enforcement becomes a string compare. Revocation is token invalidation, which is race-free.
- Per-launch minting also means a client that reconnects after a service restart gets a clean rejection instead of silently attaching to a *different* Chromium with stale target IDs.

### 4.2 Gate per frame, do not close the socket

Earlier drafts closed the agent's websocket on takeover. **Measured: that permanently bricks the browser for the agent.** One drop and the slug never recovered -- `playwright-cli list` stopped showing it entirely. Refusing frames instead was measured to survive: the same slug drove normally again the moment the lease came back. Separately, between the human's take-control and a socket close, an arbitrary number of already-sent frames still land.

- **Check the lease under `_control_lock` on every forwarded frame**, mirroring `run_action`'s existing CAS (`session.py:1735-1745`).
- Refuse **every** method when the token doesn't match the current lease holder -- not just `Input.*`. Return a CDP error, which the CLI surfaces normally, and keep the socket alive.
- This makes the per-frame check the primary guarantee and any socket close a backstop, inverting earlier drafts.

### 4.3 Discovery rewriting

`attach --cdp` begins with HTTP. **Route both the bare and trailing-slash forms** -- Playwright requests `/json/version/`, and a proxy serving only `/json/version` fails with `Unexpected status 404` and a daemon exit. Rewrite `webSocketDebuggerUrl` and `devtoolsFrontendUrl` in `GET /json/version[/]` and `GET /json/list[/]`; gate or refuse `GET /json/new|close|activate` (they bypass the websocket filter). An incomplete rewrite lets the client discover the real endpoint and route around every control here -- but note this is a **correctness** failure, not a breach: the agent has a shell and can read `DevToolsActivePort` out of the profile dir directly (§6.2).

### 4.4 Blocked methods

Refused even while the agent holds the lease.

| Method | Reason | Severity |
|---|---|---|
| `Browser.close` | Kills a browser the human may hold. **Measured: the CLI's own `close` does not send it** (Chrome survived), so this is cheap insurance against a future CLI change, not a live threat | Low |
| `Target.closeTarget` / `Page.close` on the last page | Same, one tab at a time | Critical |
| `Target.createBrowserContext` / `disposeBrowserContext` | Incognito has no profile persistence; targets invisible to the tab list and pane. **Measured: `attach` uses the default context**, so this is insurance | Low |
| `Emulation.setDeviceMetricsOverride` | Overrides the *page* viewport independently of the OS window. `window_guardian` manages X windows only and will never undo it. Load-bearing despite not being observed in testing | High |
| `Browser.setWindowBounds` | `window_guardian` re-pins every 1.0s (`window_guardian.py:31`) and real resize is X11 (`xinput.py:302`), so worst case is <=1s of wrong geometry, self-healing | Low |
| `Browser.setDownloadBehavior` / `Page.setDownloadBehavior` | Redirects downloads to an arbitrary workspace path | Medium |

**Removed from earlier drafts:** the `Target.setAutoAttach` `waitForDebuggerOnStart` rewrite -- Playwright sets that flag deliberately and pairs it with `Runtime.runIfWaitingForDebugger`; rewriting it would manufacture the exact deadlock it claimed to prevent, and our own constraint table records no deadlock. Also `Emulation.setVisibleSize` (long a no-op). Also the `Input.*`-while-unleased row, subsumed by §4.2's all-method check.

### 4.5 One piece of state, and pane-follow

The proxy keeps a `sessionId -> targetId` map from `Target.attachedToTarget`. Flattened-mode frames carry `sessionId`, not `targetId`, and both "`Page.close` on the last page" and pane-follow need the resolution. The filter is not stateless.

**Pane-follow.** `run_action` calls `_foreground_active()` after every action (`session.py:1766`) -- that single call is the entire "agent acts -> pane follows" behavior and has no other trigger. Under direct CDP there is none, and nothing fails: the human's view just goes stale. One rule, matching today's semantics: **after any forwarded command, activate the target it was addressed to, trailing-debounced ~250ms.** No method list to drift. The debounce is required -- CDP is far chattier than one call per verb.

**Pane surfacing.** `_pull_in_pane` fires on `newly_acquired` inside `_action` (`fleet.py:558`), a call site that dies with the drive verbs. `new`/`acquire`/`handoff` keep theirs, but an agent attaching to a *restored* browser calls none of them. First proxy attach is the trigger.

**Tab ordering.** `_tab_list` is documented as *"ONE ordering, the one `switch <index>` indexes"* (`session.py:974`). **Measured: `tab-list` already shows only real pages** -- Playwright filters extension `background_page` / `service_worker` / `chrome://` `browser_ui` targets itself. So only *our* `ls` needs the `type == "page"` filter, to agree with what the CLI already does. No websocket-event filtering required.

## 5. Lifecycle

### 5.1 Crash detection must not depend on anyone being attached

`_keepalive_loop` polls every 10s **regardless of whether anyone is driving** (`session.py:1017-1026`) and after two misses calls `_on_disconnected()` -- which broadcasts `crashed`, abandons both queues, and drops the manifest entry.

Neither `proc.poll()` alone nor proxy socket state covers an idle browser: the proxy has no upstream socket unless an agent is attached, and a Chromium killed by earlyoom while nobody is driving would go unnoticed. The pane would freeze on its last frame (the media path is damage-driven and has no crash awareness), the manifest would still list the browser, and it would still count against the cap of 2 -- so the next `new` fails with `FleetFullError`.

**Poll the fleet's own CDP client (§3) at the exact call site `_bu_alive()` occupies today**, with the same two-poll debounce. The debounce is meaningful on socket state, not on `proc.poll()`.

### 5.2 What a client sees

Collapse earlier drafts' five close reasons to **one generic close**. `@playwright/cli` will not surface an application-defined WebSocket close code to the agent, so a five-way signalling channel has no reader. `close` and `crash` are also indistinguishable at the socket layer and must be classified from fleet state (`close()` sets `_closed = True` before killing Chromium, `session.py:2073`).

The recovery contract is one path: **if `playwright-cli` disconnects or a command is refused, run `ls` and branch on what it reports.** `describe()` already returns `lifecycle` / `controller` / `human_pinned` / `crashed` (`session.py:2009`). The `mngr message` wake (`session.py:1304`) is unchanged.

### 5.3 The exit-code contract needs a carrier

`_render_action` maps eight daemon statuses onto five exit codes and `fleet_test.py:88` parametrizes every one, because *the exit code an agent branches on per command is load-bearing*. Exit 2 -- *stop, tell the user, end your turn, you're queued* -- is the entire takeover etiquette.

**Measured:** `playwright-cli` exits **1 on any error and 0 on success**, with no differentiation -- a lease refusal and a stale ref both return `rc=1`. Worse, a refused frame surfaces as `Execution context was destroyed, most likely because of a navigation.`, and some verbs (`snapshot`) instead hang the full 30s and return `TimeoutError`. The agent cannot tell "the human took the wheel, stop" from "your ref is stale, re-snapshot" by exit code **or** by error text.

**Fix, no new code:** the drive loop is `acquire <name>` -> branch on its exit code -> `attach`. `cmd_acquire` (`fleet.py:604`) already routes through `_render_action`, already returns the full status set with correct exit codes, already enrols the agent in the resume queue, and already pulls the pane. It also covers the `starting` / `restoring` retryable states (`session.py:1267`), which `new` returning before Chromium is up makes the *first* thing an agent hits.

**The skill must therefore say:** on **any** non-zero `playwright-cli` exit, do not interpret the message -- run `ls` (or `acquire`) and branch on what the fleet reports. That is the only trustworthy signal, and it is the same rule for takeover, crash, close, and lease expiry.

### 5.4 Service restart

Chromium does not survive it (`stopasgroup`/`killasgroup`), and `restore()` always launches fresh. Earlier drafts' "the proxy port must be stable across restarts" solved a problem that cannot arise and created a real one -- a client reconnecting to a stable URL would silently attach to a different Chromium with stale target IDs. Per-launch token minting (§4.1) makes a stale client fail cleanly instead.

### 5.5 Idle lease

60-70s real (`_LEASE_IDLE_TTL = 60`, 10s sweep), **not 90s** -- earlier drafts and SKILL.md are both stale. An open proxy socket does not refresh the lease; only a forwarded command does, so a held-but-silent session still ages out exactly as today. On expiry, refuse subsequent frames (§4.2) rather than closing.

### 5.6 Read-vs-write peek

Today `state` peeks without enrolling the agent as a waiter (`enqueue_on_busy=False`, `session.py:1683`; `browser_test.py:607`). The proxy cannot classify a CDP frame as read-only. Accept the loss: enrolment now happens at `acquire` (§5.3), which is explicit, and a bare `attach` on a busy browser is refused without enrolling.

## 6. Security

### 6.1 The SSRF guard is lost -- decision on the record

`_unsafe_navigation_reason` (`session.py:291`) blocks non-http(s), loopback, and link-local on `act_navigate`. CDP bypasses it and no filter restores it: `Runtime.evaluate` is general-purpose code execution and cannot be filtered because the CLI's `eval` and much of Playwright ride on it.

**Measured, for precision:** `@playwright/cli` blocks `file:` navigation itself (`Access to "file:" protocol is blocked`), but `chrome://version` and `http://127.0.0.1` both navigate fine, and `fetch()` from page JS to loopback succeeds. So the CLI restores the narrowest slice of the guard by accident and nothing else.

**Accepted.** The agent already has a shell and can read `/home/user/.mngr/env` or curl the metadata IP directly; the guard never protected the key from the agent. **Then delete the function** unless a live human- or page-originated caller remains after `act_navigate` dies -- a guard with no caller is worse than none, because the changelog would claim a protection nothing is wired to.

### 6.2 The proxy is a guardrail, not a boundary

Earlier drafts claimed the proxy is "the only thing that may reach Chrome's debug port." **False** -- the profile dir is at `/home/user/.mngr/browser-profiles/` (`session.py:432`), agent-readable, and `DevToolsActivePort` inside it hands over the real port. The proxy defends against an *obedient* agent and against playwright-cli's own reconnect behavior. Size the block list for what the CLI actually sends, not for a hostile client. If a real boundary is ever needed, the answer is `--remote-debugging-pipe` (no TCP port exists at all); note it, do not build it.

### 6.3 Extensions: pinned and vendored

Replace browser-use's unpinned runtime download from `clients2.google.com` with vendored, version-pinned unpacked extensions in the Fortress env.d unit, loaded via `--load-extension=`.

- **Keep** uBlock Origin Lite (`ddkjiahejlhfcafbddmgiahcphecmpfh`) and "I still don't care about cookies" (`edibdbjcniadpccecjdfdjjppcpchdlm`).
- **Drop** Force Background Tab (`gidlfommnbibbmegmgajdbikelkdcmcl`) -- it fights the pane's active-tab follow. It loads from a cache dir, not the profile, so dropping it takes effect cleanly.
- **Extension IDs are path-derived.** Moving the directory changes the ID and orphans uBlock's stored settings in existing profiles; set `"key"` in the vendored `manifest.json` to pin the ID.
- Ship independently of the rest of this plan.

### 6.4 Stale refs are weaker than stale indices

`_selector_map` is cleared after every mutating verb, so a stale `click <i>` is *guaranteed* refused (`session.py:1800`; `test_browser_integration.py:646`). Playwright refs survive same-page DOM mutation -- a stale ref after a re-render can resolve to a *different* element. Refusal becomes "usually errors," in a browser holding live logins. No cheap proxy-side fix: put "re-snapshot after every action" in the vendored skill and record it here.

## 7. The agent-facing surface

```
$ uv run agentic-browser-fleet new
-> started browser browser-1
   drive it:  playwright-cli -s=browser-1 attach --cdp=http://127.0.0.1:<port>/browser-1/<token>
```

- Pin `ARG PLAYWRIGHT_CLI_VERSION=0.1.18` in `system/Dockerfile` + `npm install -g "@playwright/cli@${PLAYWRIGHT_CLI_VERSION}"` in `setup_system.sh`, matching `CLAUDE_CODE_VERSION` / `CODEX_VERSION` / `PI_VERSION` / `LATCHKEY_VERSION`. It is `@playwright/cli`; bare `playwright-cli` on npm is a deprecated stub.
- **`PLAYWRIGHT_CLI_SESSION` cannot be a static env var** -- names are minted per `new` and an agent may hold two. Export per-`new` or keep `-s=` explicit.
- **Slug hygiene:** a slug is poisoned if its daemon is detached or killed, and `close` retires a fleet name that `new` can re-mint. The skill must use a fresh slug per attach (`-s=<name>-<epoch>`) or `close` must clean the playwright-cli session dir. Name the owner of that cleanup.
- **Vendor the CLI skill into `.agents/skills/`** rather than `playwright-cli install --skills` at build, so agent instructions stay in git. It must mandate `--filename=` / `find` over bare `snapshot`, teach `detach` not `close`, and teach re-snapshot-after-every-action (§6.4).
- The fleet skill shrinks to ownership: `new`, `ls`, `close`, `acquire`, `release`, `handoff`, the exit-code table, takeover etiquette. **Fix its three stale "90s" references** to 60s.

## 8. Pre-existing bugs to fix along the way

Found while reviewing; all live today, independent of this plan.

### 8.1 Orphaned Chromium on an unexpected service restart -- two Chromiums, one profile

`autorestart=true` (`supervisord.conf:371`) restarts the daemon *without* group-signalling on an unexpected death, orphaning Chromium and Xvfb. `restore()` then calls `_clear_stale_singleton` (`session.py:823`), deleting the lock files the **still-running** old Chromium holds, and launches a second Chromium on the same `user_data_dir`. `_display_is_free` (`session.py:140`) sees the orphan's `/tmp/.X{N}-lock` and picks a new display, so nothing collides loudly -- you just get double the memory and one profile with two writers.

Worse, OOM retagging cannot reach the orphan: `oom_retag.sweep_once` walks strict descendants via `/proc/<pid>/task/*/children` (`proctree.py:14`), so reparented renderers keep Chrome's self-written `oom_score_adj = 300` instead of being raised into `SHARED_BROWSER`. Under pressure earlyoom sheds **the agent before the browser** -- the exact inversion `oom_retag.py` exists to prevent.

**Fix:** on startup, reap orphaned Chromium/Xvfb belonging to known profile dirs before restoring, and refuse to launch onto a `user_data_dir` whose singleton lock is held by a live pid.

### 8.2 `DevToolsActivePort` is not cleared with the other singleton files

`_clear_stale_singleton` (`session.py:468`) removes `SingletonLock`/`SingletonSocket`/`SingletonCookie` but not `DevToolsActivePort`, which persists from the previous run. Harmless today (nothing reads it); a stale-port race the moment §2 does. One-word diff to `_SINGLETON_LOCK_NAMES` (`session.py:449`).

### 8.3 SKILL.md documents a 90s idle lease; it is 60s

Three places. `_LEASE_IDLE_TTL = 60` since it was deliberately lowered.

### 8.4 `cmd_lock` / `POST /hold` are undocumented

Reachable, tested, and mentioned nowhere in SKILL.md. Either document or delete (§1 deletes).

### 8.5 `anthropic_key_status` advertises a verb that does not exist

Its message names `extract`, which has no implementation anywhere.

### 8.6 The scroll defect that prompted this work

`skill_cli`'s `scroll` is `window.scrollBy`, a no-op wherever content lives in an inner scroller, and it reported `ok` regardless. A fix exists (hit-tested CDP `mouseWheel` at a point, `--at` targeting, before/after position so a no-op stops reporting success). Land it or supersede it per §9.1 -- do not lose the behavior silently.

## 9. Open decisions

### 9.1 Does `scroll` survive as a fleet verb? -- RESOLVED: no

**Measured.** On a 200-row virtualized list (only ~18 rows in the DOM at a time), six plain
`playwright-cli mousewheel 0 800` commands walked the rendered window `[0,18] -> [20,38] -> [40,58]
-> [80,98] -> [100,118] -> [120,138]` -- deterministic 20 rows per wheel, no hover, no `--at`
targeting, no custom verb. `find` confirms cheaply whether a row has materialized without printing
the snapshot.

So the reported bug is fully served by the stock CLI, and the custom `scroll` verb does **not**
survive. Delete it with the rest of §1.

Two caveats to carry into the skill rather than into code:
- The wheel lands at the **last mouse position, default (0,0)** -- top-left. On a two-pane layout
  that is the sidebar, not the main pane, and it scrolls the wrong container silently. Teach
  `hover <ref>` inside the intended pane first.
- `mousewheel` reports nothing about whether anything moved. Teach `find` (cheap) or a one-line
  `eval` (exact) rather than a full `snapshot` (48k bytes) to confirm progress.

### 9.2 `task`'s real loss is context budget

`run-code` is more capable for the cases SKILL.md reserved `task` for. What goes away is that `task` ran a *separate* LLM on the user's key with its own step budget and streamed a compact trace -- a 50-step flow cost the parent a few hundred tokens instead of 50 snapshots. Not a lost capability; a context-window regression on long flows. Not a blocker.

## 10. Phasing

**Phase 0 -- DONE.** All five questions answered against `@playwright/cli` 0.1.18 + Chrome 151 with
a working prototype proxy; results are in the constraints table and folded into §4.2, §4.4, §4.5,
§5.3, §6.1 and §9.1. Headlines: the architecture works end to end; refusing frames is safe and
dropping the socket is not; `close` does not kill the browser; the virtualized-list case needs no
custom verb.

**Phase A -- the proxy, alongside the existing verbs.** Build against today's browser-use-launched Chromium, whose CDP URL browser-use already exposes. Ship `@playwright/cli` pinned, the vendored skill, and the rewritten fleet skill. Launch untouched, fully reversible, real usage moves over. Earlier drafts put the launch swap first, which forces throwaway work: `ActionHandler` needs a `BrowserSession` (`session.py:1677`), so browser-use would have to be rewired into connect-to-existing mode purely to be deleted later.

**Phase B -- delete the verbs and browser-use.** That deletion *forces* the launch change (§2) and the fleet's own CDP client (§3), so both land where they are actually needed.

**Independent, ship anytime:** §6.3 extensions, §8.1-§8.5 pre-existing bugs.

## 11. Verification

- **Stealth.** `navigator.webdriver === false` and both extension IDs present after launch. Earlier drafts had no stealth check at all, which is what would have let §2's regression ship silently.
- **Takeover -- the acceptance criterion.** Agent attached mid-session; human takes control; the next forwarded frame is refused; a re-attach with the old token is rejected; the agent's `acquire` returns exit 2.
- **Agent-vs-agent.** Agent B cannot drive agent A's browser: no token, no access.
- **Bypass.** No proxy response body contains Chrome's real debug port.
- **Idle crash.** Kill Chromium with nobody attached; within two keepalive ticks the pane shows crashed, the manifest entry drops, and the slot frees for `new`.
- **Geometry.** `[innerWidth, innerHeight, devicePixelRatio]` unchanged across attach, drive, detach -- and after launch, confirming no `no_viewport` regression.
- **Tab ordering.** `ls --include-tabs` index N is `tab-select` index N.
- **Pane-follow.** Agent navigates and switches tabs over CDP; the pane follows.
- **Inner-scroller.** A page whose body does not scroll and whose content is a virtualized inner list: the agent reaches the bottom. This is the user-visible reason the work exists.
- **Restart.** Service restart; a stale client fails cleanly rather than attaching to a different Chromium; no second Chromium on the same profile (§8.1).

# Flow executor: box-side browser on the forwarded origin

## Purpose and scope

This spec defines the replacement executor for `ui_flows` in the minds_evals harbor persona evals: a box-side Playwright browser driving the delivered app's **forwarded origin** (`https://<label>.agent-<hex>.localhost:<port>/`, where the plugin serves it), served by the `mngr forward` plugin.
It replaces the v1 executor -- the workspace's own browser fleet -- whose two structural defects are recorded in [outcome_verification.md](outcome_verification.md), Level 4: coupling the eval to the workspace's internal-tool security model, and reaching the app under the product (raw in-container socket) rather than through it.
Everything around the executor is out of scope and unchanged: the host-side verification-agent LLM loop, the `ui_flows` schema, the evidence bundle shape, the failure taxonomy's failed-vs-error semantics, the judge pre-step, and scoring.

Ships as one PR stacked on the Phase 2 flows PR (#523), branch `maciek/minds-evals-forwarded-origin-flows`: it swaps the executor, deletes the fleet command layer, and removes the eval's dependency on dwt PR #462's allowlist (that PR's guard *hardening* remains independently worth landing).

## The serving mechanism this builds on

`mngr forward` (`libs/mngr_forward`) is a standalone mngr plugin; minds itself consumes it as a spawned subprocess, so nothing here depends on the Minds application.

- It binds `127.0.0.1:<port>` (8421 by convention; minds runs it with TLS + HTTP/2) and routes by **Host header**, on origins keyed by **agent id**: `<service>.agent-<hex>.localhost:<port>` reaches that agent's registered service, the bare `agent-<hex>.localhost:<port>` origin reaches the default service (system_interface). Legacy `host-<hex>` coordinates still parse, but the plugin only redirects HTML navigations off them and refuses everything else.
- The service labels are the values `forward_port.py` registers in the workspace's `data/.state/apps.toml` -- the registry the evidence collector already captures, so flow-target discovery reuses the delivered-apps resolution (internal rows and isolated-instance rows excluded).
- Remote workspaces are reached over a **per-host SSH tunnel** -- mngr's own transport, the same one the eval's exec and rsync already ride, so Modal workspaces work by construction.
- `*.localhost` needs no DNS (browsers hard-resolve it to loopback).
- Auth is a session-cookie gate with programmatic bypasses built for consumers: `--preauth-cookie <token>` pre-arms a session the browser presents as the `mngr_forward_session` cookie, and `--browser-bridge-token` + `/_bridge?token=...` serves plain browsers.

## Design

### Browser lifecycle: launch once, connect per step over CDP

The box image gains Playwright and its pinned Chromium.
At the start of the flow phase the runner launches one headless Chromium with a remote-debugging port; each step then runs as one `environment.exec` of a short step script that connects over CDP, performs the batched action + DOM digest + screenshot, and prints a JSON result.
This keeps the proven one-exec-per-step model (the host-side LLM loop is unchanged) while browser state -- cookies, storage, the open page -- persists across steps in the long-lived Chromium, with no long-lived command protocol of our own.
The step scripts ship as package resources written into the box at trial time (the `box_reverse_tunnel.py` pattern), not baked into the image, so iterating on them never invalidates the image cache.

Per-step latency budget: the workspace hop is gone from the action path (browser actions are box-local against the forward proxy; only the proxy's tunnel touches the workspace), so the expected floor is box-exec latency plus render time.
Measured on the live proof trial (dwt main, no branch override): **~4 s per step** (17 steps across two flows in 116 s, versus the fleet executor's ~20-30 s/step), with both flows passing and zero instrument-shaped errors -- budgets are sized from that measurement, not carried over.

### The forward instance: driver-owned, minds-configured

The driver starts its **own** `mngr forward` instance in the box for the flow phase, rather than discovering the one the headless minds backend may have spawned: a driver-owned instance has a known port, a known `--preauth-cookie` token minted per trial, and no coupling to backend internals or its cookie.
It is configured the way minds configures its spawn -- the spawn lives in the desktop client's `forward_cli.py`, and TLS + HTTP/2 arrive via its `--use-http2` switch (the plugin is plain HTTP without it) -- so the exercised path matches production; the step scripts run the browser context with certificate errors ignored (the cert is self-signed local machinery, not the thing under test).
Flag parity is pinned by test against that spawn's source, with exactly two excused differences: the eval passes `--port` (a driver-owned instance needs a known port; minds lets the plugin pick) and omits `--embedder-origin`/`--reverse` (both shape how minds *embeds* the app, and the origin surface has nothing framing it); a new minds flag cannot silently join the excused set.
The instance is scoped to the trial's own `USER_ID` like everything else in the box, started when the flow phase begins and stopped in the phase's cleanup; its stdout JSONL (readiness, tunnel events) is captured into the collector's trace.

### Target resolution and auth choreography

- Among several delivered apps, the flow target prefers one that **answered its root-path probe**; registry order decides only among equally reachable apps and is the fallback when nothing was probed -- a row whose port is dead serves the proxy's error page, and driving it would record the deliverable as broken without ever reaching it.
- The flow target for surface `"origin"` is `https://<label>.agent-<hex>.localhost:<forward-port>/`, where `<label>` comes from the delivered app's registry row (the `label` field) and `agent-<hex>` is the workspace's agent id, which the driver already holds.
- Before the first navigation, the step script installs the pre-auth session cookie into the browser context (Playwright sets cookies programmatically, so the `/_bridge` redirect path is unnecessary). It is installed at the scope the plugin issues its own session at -- `Domain=agent-<hex>.localhost`, covering the bare origin and every service label under it -- so a flow that crosses labels stays authenticated.
- One browser context per flow, fresh: flows must not leak state into each other; persistence-across-reload is exercised *within* a flow by reloading, and the origin-scoped cookie/session behavior of the app itself is part of what this executor newly makes testable.

### Evidence: same shape, simpler transport

- The per-step DOM digest is Playwright's `page.aria_snapshot(mode="ai")` -- a YAML-shaped ARIA tree as a string -- plus URL and title, recorded verbatim in `flows/<name>/log.jsonl` exactly where the fleet's browser_use digest went.
  (The older `page.accessibility.snapshot()` API this spec first assumed is fully removed from Playwright, verified empirically at 1.59-1.62; the replacement reshaped the action vocabulary for the better -- elements are addressed by **ARIA role + accessible name**, which survives page changes, where the fleet's numeric indexes went stale on every mutation and forced a re-read before each action.)
- Screenshots are written box-side by the step script and are already on the box filesystem -- no workspace staging or rsync leg at all; the collector moves them into `/logs/agent/verification/flows/` directly.
- `manifest.json` is untouched. The judge pre-step flattens the same evidence, now attaching each flow's last four screenshots (24 in all) and presenting each flow's declared steps, its `expect`, its completion and the agent's reading of the final page.
- The trial-time outcome of a flow is COMPLETION -- `completed` or `incomplete` -- and the grade-time judge alone rules on the `expect`; the programmatic criterion is `ui_flows_completed`.

### Failure taxonomy: executor-level reasons replace fleet-level ones

Status `error` (the harness could not find out) gains executor-specific reasons, each distinct so an infrastructure regression is diagnosable: `browser_launch_failed`, `cdp_connect_failed`, `forward_unreachable` (the proxy itself), `tunnel_down` (proxy up, workspace leg dead), `tls_refused`, `workspace_unaddressable` (an agent id the plugin does not route, so no origin can be built).
Status `failed` (the workspace fell short) is unchanged: the app not answering on its forwarded origin, an expect not met, a per-step deadline exhausted by a hanging app.
The fleet-specific reasons (fleet CLI dead, Chromium first-boot missing, slot/lease contention) are deleted with the fleet layer.

## Surfaces

A `ui_flows` entry carries an optional `surface`:

- `"origin"` (default, this spec): straight to the app's forwarded origin -- the iframe's `src`. One origin, the app's own DOM, no frame-piercing.
- `"minds-ui"` (reserved, rejected-loudly until implemented): drive the Minds client UI at the bare `agent-<hex>.localhost` origin and reach the app as an embedded iframe in the workspace chrome.
  The same executor serves it -- only the entry URL and a frame-piercing layer differ -- and it is the only surface that can catch works-at-origin-but-broken-when-iframed failures or exercise minds-level login and tab UX.
  Deferred until an origin-surface run motivates it; note the bare-origin UI in a plain browser may diverge from the Electron composition, which is a verification item for that surface, not this one.

## Packaging

- Playwright + Chromium install goes into the box image (`environment/Dockerfile`): the environment tree stays byte-identical across a dataset's tasks, so the image-cache discipline holds; expect roughly +400 MB image size and a one-time build-cost bump.
- The Playwright version is governed by the **box's** venv, not the host's: the step scripts execute in the box against the root-workspace venv (where `playwright` already arrives via `apps/minds`), and the Dockerfile's `python -m playwright install chromium` in that same venv installs the browser revision matching that package -- so the scripts and the browser cannot drift by construction, and no minds_evals-side pin is needed.
  With PR-459 landed, `apps/minds_evals` is a standalone uv project outside the root workspace; a Playwright pin there would govern nothing the box runs and is deliberately absent.
  The box venv's Playwright sits at 1.59.0 under the root's `exclude-newer` cooldown; newer releases postdate it and cannot resolve.

## What this un-blocks operationally

- Live flow trials no longer point `dwt_branch` at the dwt PR #462 branch: with the fleet out of the loop, flows run against dwt main (or the pinned SHA once pinning lands).
- The eval's only product dependency for flows becomes `mngr forward` -- versioned in this repo alongside the eval, so a breaking change to the forward plugin shows up in the same review, not as a silent remote regression.

## Verification plan

1. Unit tests: forward-instance command construction (flag parity with minds' spawn asserted against the minds code that builds it), the minted origin parsed by the plugin's own Host-header pattern, target-URL resolution from captured registry fixtures, step-script JSON contract, taxonomy classification per failure reason, cookie scope and installation.
2. Oracle: unchanged (the fabricated bundle carries flow verdicts, no browser involved) -- one run to confirm nothing regressed.
3. Live proof trial, same bar as the fleet executor's: a real todo-app case on **dwt main**, at least one flow verdict recorded from a live run, per-step timings captured (establishing the new budget baseline), judge consuming digest and screenshots, and zero instrument-shaped errors.
4. Smoke items, all settled during implementation (results folded into the sections above): the forward spawn lives in the desktop client's `forward_cli.py` with `--use-http2` as the TLS switch; the registry field is `label`; every Chromium system dep was already in the box image; and the assumed accessibility-snapshot API is removed from Playwright entirely -- `aria_snapshot(mode="ai")` with role+name addressing replaced it.

## Open questions

1. **Forward-port reuse vs fresh port**: a driver-owned instance on a fresh port is the default here; if the backend's own instance proves discoverable and stable, reusing it would exercise one less divergence from production. Decide on evidence from the first live run.
2. **Digest fidelity**: whether Playwright's accessibility snapshot matches browser_use's digest quality for the LLM loop; if flows get lost, an element-annotated DOM extraction becomes a follow-up.
3. **Chromium-in-image vs first-boot install**: the image install is chosen for determinism; if image size becomes a problem, revisit -- but never with a skippable one-shot (the fleet's install mode was one of its unavailability classes).

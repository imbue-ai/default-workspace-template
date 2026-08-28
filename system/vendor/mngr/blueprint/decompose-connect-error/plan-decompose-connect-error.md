# Decompose CONNECT_ERROR and stop misdiagnosing the workspace

## Overview

* `CONNECT_ERROR` currently conflates three evidence classes: the workspace-side didn't answer, this device cannot connect (issue #427's tunnel trust-material failures), and the forward's own connection pool is exhausted (PR 338). The recovery pipeline reads all three as "workspace sick," so a healthy workspace gets blamed, "restarted," and shown a RESTART_FAILED card.

* Decompose the reason as first-class enum values. The producer (`mngr forward`) and consumer (minds) ship pinned to the same commit in every distribution mode, so PR 338's cross-version-contract concern does not apply; both sides land together in this one PR.

* Ride PR 304's existing rails: record the classified cause on the health tracker episode-scoped, and surface it as a verdict that outranks the restart episode's claims on the card, notice band, and machines list.

* The fix is presentation-only: unattended restart dispatch stays evidence-free (a start-only attempt cannot hurt a live container, so it might as well try). What changes is what the surfaces claim.

* Independently of classification, surfaces stop claiming "Restarting" without evidence: host-state evidence gates the copy, and `mngr start`'s already-computed `was_host_started` is plumbed through so a no-op start becomes affirmative evidence that the workspace, not a restart, is the problem.

* The recovery exec probe (`recovery_probe.py` and the `/workspaces/<id>/health` route) has had no consumer since the SPA port, and its unique signals are either covered by this decomposition or by the dispatched start's outcome — so it is deleted outright, and PR #305 (which would have rewired it) is closed as superseded.

## Expected behavior

* #427 scenario (deterministic local tunnel refusal, e.g. missing known_hosts / key material): the card and band say "This device cannot connect to the workspace," with the verbatim error text behind an expandable details affordance. The card offers a Restart App button (which definitively fixes pool exhaustion and re-spawns the forward) instead of Restart Machine. This verdict outranks RESTARTING / RESTART_FAILED and clears as soon as a request or probe succeeds.

* Pool exhaustion surfaces the same device-side card (no separate user-facing segmentation); the recorded cause distinguishes tunnel vs pool in a Sentry breadcrumb so recurrence is measurable. Forward self-heal (bouncing the forward subprocess) stays deferred unless that breadcrumb shows recurrence.

* Dial failures against a vanished host, timeouts, loopback refusals, and WS handshake failures keep today's behavior: enroll a probe suspect, recover as before. Classification is conservative — only failures provably raised before any network I/O count as device-side.

* A stuck workspace whose host trustworthily reads STOPPED keeps "Bringing {machine} back online…" (already shipped in PR 304). When the host reads RUNNING or the snapshot isn't trustworthy, the card says "Reconnecting to {machine}…" and the machines-list badge says "Reconnecting…" — matching the notice band's existing standard — instead of claiming "Restarting."

* A user-initiated restart keeps "Restarting {machine}…": the user's own action makes the claim honest.

* When the dispatched `mngr start` reports it did not actually start anything (`was_host_started` false) and probes keep failing, the episode's terminal state reads as the workspace being unresponsive — reusing the existing "not responding" phrasing — instead of "Restart failed." A real cold start (`was_host_started` true) keeps the restart framing. The tracker still enters the same `restart_failed` terminal state either way — the reframe is render-only — so the recovery card's auto-raise on that edge (and everything else keyed to the state) is unchanged.

* The loopback-refusal shape (SSH tunnel to the host established, but the `direct-tcpip` channel to the inner port was refused) is reported as its own reason: "the host is reachable; its server isn't listening." It is recorded episode-scoped like the other causes so logs and Sentry can tell a dead service inside a reachable container apart from an unreachable host — no UI or enrollment change on day one. This passively preserves the best signal the deleted exec probe's LISTEN scan provided, at every ~1/sec retry instead of a one-shot exec.

* The recovery-diagnostics exec probe never runs (it already never ran in production — nothing fetches `/health`); recovery relies on passive evidence plus the dispatched start's outcome. No user-visible behavior changes from the deletion itself.

* An unknown reason value in a failure envelope no longer drops the envelope: it is treated as a generic connection-class failure and still enrolls, so same-version pairing is belt-and-suspenders rather than load-bearing.

* Enrollment behavior is unchanged for every reason — probes stay the arbiter. `RESTARTING` as a tracker state keeps doing its real work (probe-target exclusion, operation claim); only what surfaces render changes.

## Changes

* `libs/mngr_forward` — split `SystemInterfaceBackendFailureReason`: add `POOL_EXHAUSTED` (the `httpx.PoolTimeout` sites), `TUNNEL_SETUP_FAILED` (deterministic pre-dial local refusals only), and `BACKEND_NOT_LISTENING` (the loopback-refusal sites, HTTP and WS). The tunnel layer tags which phase failed so the server does not guess from the exception type; everything else stays `CONNECT_ERROR`. Rewrite the enum docstring to match.

* `libs/mngr_forward` — add an optional `detail` field to `SystemInterfaceBackendFailurePayload` carrying the verbatim error text, which today never crosses the envelope boundary.

* `apps/minds` consumer (`forward_cli.py`) — parse the reason leniently: unknown values map to a generic connection-class failure instead of dropping the envelope.

* `apps/minds` health tracker — record the classified cause and detail episode-scoped (the `record_backend_outage` pattern), idempotently (envelopes fire on every ~1/sec retry); enrollment gate treats the new reasons like `CONNECT_ERROR`.

* `apps/minds` recovery/verdicts — a device-side verdict from a recorded `TUNNEL_SETUP_FAILED` or `POOL_EXHAUSTED`, outranking the restart episode's conclusion the way `BACKEND_UNREACHABLE` does, cleared on success; emit a Sentry breadcrumb naming the cause.

* `apps/minds` frontend — the device-cannot-connect card (copy, expandable detail, Restart App via the existing `restartApp()` bridge, no Restart Machine); evidence-gated copy in `RecoveryCard.ts` and the `LandingPage.ts` badge ("Reconnecting…" when there is no evidence of an actual restart); terminal-state copy reframed to "not responding" on a no-op start.

* `libs/mngr` — `mngr start` structured output reports `was_host_started` (already computed by `ensure_host_started`, currently discarded); the minds recovery dispatch switches to structured output and reads it. Named for the host rather than a bare `was_started` because that is the distinction that makes it useful: a start always starts the named agent, and what a caller needs to know is whether a *host* was booted.

* Probe deletion — remove `recovery_probe.py`'s exec machinery (the in-container script, argv builders, probe-row model and parsing), the `/workspaces/<id>/health` route and `probe_workspace_health`, and the resolver-snapshot mirror in `forward_cli.py` (its only consumer); drop the `ResolverSnapshotPayload` emission from `mngr_forward` in the same change (producer and consumer ship pinned). Keep the passive classifier that backs `read_backend_unreachable_verdict`, trimmed to the inputs it actually reads. Close PR #305 as superseded, pointing at this PR.

* Mechanics: merge `origin/main` first (PR 476 touched the same recovery files); changelog entries for `libs/mngr_forward`, `libs/mngr`, `apps/minds`, and `dev` (this plan file).

## Acceptance criteria

* Forward-layer unit tests map each exception class to the expected reason and detail, including both `SSHTunnelError` phases: pre-dial local refusal yields `TUNNEL_SETUP_FAILED`; a dial failure against a vanished host stays `CONNECT_ERROR`; a refused loopback channel over an established tunnel yields `BACKEND_NOT_LISTENING`.

* Tracker records the `BACKEND_NOT_LISTENING` cause and its enrollment behavior matches `CONNECT_ERROR`; no verdict or copy change asserts on it.

* The probe deletion leaves no production caller of the exec probe: the `/health` route, `probe_workspace_health`, and the resolver-snapshot plumbing are gone along with their tests, and `read_backend_unreachable_verdict`'s behavior is unchanged.

* Consumer test: an unknown reason string still enrolls a suspect as a generic connection failure.

* Tracker tests: repeated envelopes record one cause per episode; the cause clears on success.

* Verdict tests: the device-side verdict outranks RESTARTING / RESTART_FAILED and clears on probe success.

* Frontend tests: copy selection by evidence (host offline vs running/untrusted vs manual restart), the device-side card body, Restart App button, and expandable detail.

* `mngr` test: `start --format json` reports `was_host_started`; minds test: a no-op start with continuing probe failures reframes the terminal copy to "not responding."

* Manual verification: a synthetic #427 repro (key path with no known_hosts sibling) drives envelope, tracker, recovery info, and card end-to-end at least once locally.

* Full suite green in CI; changelog entries present for all four touched projects.

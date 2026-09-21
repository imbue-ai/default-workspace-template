# Proxy calibration and #925: findings from the first cut

**Status:** notes, not a spec. The self-diagnostic suite (`spec.md`) no longer runs the in-box proxy; these findings are kept for the later family of checks that calibrates the proxy against the transcript, per harness, and looks for requests the transcript does not account for (stray agents, ancillary models). They were measured on 2026-09-14 with the first cut of the suite, whose cells ran with `--ak proxy=true`.

## What the proxied cells showed

- On a trial with no subagent, every transcript step matched one proxy record, bucket for bucket, on `haiku` and on `opus[1m]`; the proxy carried one further request per trial the transcript does not have, on the chat's current model.
- On the behaviour cell the proxy also held the worker's requests, which the transcript account reaches only through the worker capture; a claude worker ran `opus[1m]` whatever the lead was switched to (#1008, #1009), and `is_proxy_model_confirmed` excused it because it excuses any model equal to the greeting's.
- The proxy's usage hook logged successful requests only until the first cut added failed-request records (`async_log_failure_event` in `resources/box_proxy_hooks.py`), which is how a routing failure (a 400 for a model the proxy cannot route) became observable.
- The proxy meters the `anthropic` lane only; per-harness spend calibration needs a metering source per lane.

## The section as it stood in the first cut of the spec

## The proxy: calibration and #925

The fixture trial and the claude behaviour cell run with `--ak proxy=true`.

**Calibration.**
The proxy meters every request the workspace makes, and the transcript prices the chat's own steps, so on a trial with no subagent every transcript step must match one proxy record, bucket for bucket.
Measured on mngr `main@4cf34af6`, dwt `main@96935db5`, on `haiku` and on `opus[1m]` at standard speed: the greeting and the reply each matched a proxy record exactly in all four token buckets, and the proxy carried one further request the transcript does not have.
Tokens are compared, never dollars, so check-time pricing (PR #959) changes nothing here.
On the behaviour cell the proxy also holds the worker's requests, which the transcript account reaches only through the worker capture, so the calibration there is the capture's completeness as well.

**#925.**
`is_proxy_model_confirmed` requires every metered model other than the requested one to be the greeting's, and a proxy record carries no attribution.
Every routable probe made exactly one request the transcript does not have, and it ran on the chat's current model rather than on a third one:

| requested | turn | proxy records (model, speed, input/output tokens) | `is_model_confirmed` |
|---|---|---|---|
| `haiku`, effort `medium` | a literal reply | greeting `claude-opus-5` fast; reply `claude-haiku-4-5-20251001` standard; ancillary `claude-haiku-4-5-20251001` standard, 912/10 | `true` |
| `opus[1m]`, effort `low`, `fast: false` | a literal reply | greeting `claude-opus-5` fast; reply `claude-opus-5` standard; ancillary `claude-opus-5` standard, 1191/14 | `true` |
| `opus[1m]`, effort `low`, `fast: false` | one `Bash` call (`date -u`), then a reply | greeting `claude-opus-5` fast; two turn requests `claude-opus-5` standard; ancillary `claude-opus-5` standard, 1189/12 | `true` |

So a one-turn trial does not reproduce #925, and a single tool call does not change that.
The ancillary Haiku requests #925 describes came from a 328-request roadmap trial with workers.
The working hypothesis is that an Opus session sends Haiku side-calls for the harness's own metadata collection and classification, and that none of their triggers occurs in a turn this small; the template runs Claude Code with permission prompts bypassed (`skipDangerousModePermissionPrompt`), which would skip any classification of a command before it runs (unmeasured).
Both families assert confirmation with no known-failure mark, so #925 appearing is a failed fact, and its reproduction is a follow-up (milestone 14).

**The fixture's harness config** is `haiku` at effort `medium`, `fast: false`: the one measured config whose switch is visible in both halves of the record (the transcript names a reply model other than the greeting's, and the proxy shows the greeting fast and the reply standard), routable by the proxy on main, and the cheapest arm measured.


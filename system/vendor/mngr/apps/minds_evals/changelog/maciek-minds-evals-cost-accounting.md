A run's cost is reported where a run is read. `minds-evals check-run` reads each trial's
`agent/usage.json` and carries what it finds into both summaries. Nothing gates on any of it: a cost
is not a shortfall, and the pass/fail verdict does not depend on it.

`agent/usage.json` carries a third block, `verifier_agent`, beside `workspace_agent` and `decider`:
the UI-flow verification agent's own spend, beside its copy in the trial metadata. Like the other two
it covers the whole trial -- on a multi-step trial it is summed over the steps that ran a
verification phase -- and it is `null` where no step ran one, so a reader can tell a phase that did
not run from a file written before the block existed.

A trial records tokens and nothing else; `check-run` is where money exists. `agent/usage.json`, the
trial metadata and the ATIF trajectory carry token counts per model and the model ids that price them,
and no dollar figures at all -- so a provider changing a price cannot rewrite trial data. `check-run`
prices each spender from those tokens, from litellm's own `model_cost` map -- read once per report, so
every figure in one report is priced at one set of rates -- and both summaries say which map answered (`priced by litellm 1.93.0 (remote price map)`; litellm refreshes that map over the
network at import and falls back to the copy its wheel ships, so the version alone would not pin the
rates). The same run checked again after a price change reports different money from the same tokens,
which is the point.

Two consequences to know about. Harbor's `cost_usd` and the trajectory's `total_cost_usd` are left
unset, so the **viewer shows no cost for a trial**: `check-run`'s summaries are where a figure
lives. And no block of `agent/usage.json` carries a dollar figure. What every block does carry beside
its tokens is what says how far a figure derived from them can be read (`is_cost_complete`,
`is_cost_rate_certain`, the per-model fast-mode subset), since those describe traffic rather than
prices.

Reading the live map buys three things no hand-kept table could express. A claude
trial's prompt-cache writes are priced at Anthropic's 1-hour rate, since Claude Code asks for the
1-hour cache, which costs 2x an input token against the 1.25x of a 5-minute write. A pi arm on
OpenRouter is priced at the gateway's own rate: pi reports a model without the gateway that billed it
(`openai/gpt-5-mini`), and the id the arm asked for (`openrouter/openai/gpt-5-mini`) is what the map
holds the rate under. And any model litellm prices has a figure, `claude-opus-5` and
`claude-fable-5-1` among them. Unpriced means litellm's map holds no such key, or holds a placeholder
row whose every rate is zero (`zai/glm-4.7-flash`), and unknown is never partial: one unpriced model
leaves the whole spender without a figure and names the model.

The eval's in-box LiteLLM proxy routes every claude model through one `claude-*` pattern entry, the
way the deployed proxy does, and leaves pricing to litellm's map inside the box -- so a model is
routable and priced there the day the map carries it. Its per-request `cost_usd` in
`agent/usage_proxy.jsonl` is litellm's own standard-rate figure for that one request, the proxy's
observation of what it served, which nothing sums.

The project declares the two packages it imports for this directly: `litellm`, whose map prices every
figure, and `imbue-mngr`, whose trajectory builder and agent-lifecycle primitives read a captured
workspace transcript. It no longer depends on `imbue-mngr-usage`, whose table priced nothing here any
more.

The markdown summary carries two columns after `reward` -- `agent cost` for the workspace agent under
test, and `harness cost` for the decider and the verification agent summed, which are one source
(host-side calls priced the same way). A totals line above the table says what the run spent
and what that sum covers (`agent spend $23.68+ over 2 trials, 1 unknown; harness spend $0.35 over 2
trials`). A cell reads `-` where the trial recorded nothing for that spender, which is not a cost of
zero, and `<$0.01` where a real figure is too small to print at that width. Two marks qualify a
figure, and a legend under the table explains them wherever one appears: `+` is a floor -- traffic
the total does not hold (delegated calls, or a harness call that came back with nothing and may have
been billed anyway), or a speed tier nobody observed, so fast-mode traffic is priced at the standard
rate -- and `?` is spend nothing could price, naming the models responsible where the trial recorded
them. A trial with an unpriced model is counted in a total but contributes no figure to it, so no
total is a partial sum passed off as a complete one.

The JSON summary carries the same records per trial under `spend` -- one entry per spender, with
`cost_usd`, `is_complete`, `is_rate_certain` and `unpriced_models` -- plus a `pricing` block naming the
map those figures came from.

A stepped trial is read from its steps. Harbor moves `agent/` and `verifier/` into
`steps/<name>/` after every step, so `minds-evals check-run`, `minds-evals check-diagnostics` and
`minds-evals cleanup-environments` resolve each artifact through one layout resolver: the driver's
`state.json` and `usage.json` accumulate across the steps, so the trial's state and the tokens its
spend is priced from are the last step's, while every step's own evidence manifest and reward details
are read, so a gate that failed or an evidence entry left unmeasured on any step fails the trial. A
trial that died mid-step, before harbor archived that step's outputs, is read from the copies left at
the trial root, so its spend is reported and the Modal environment it named is deleted. Entry ids and judge criteria
carry the step they came from (`build/http_0_root_0`, `amend/conciseness`), the `completed` cell says
how many steps a pass covers (`pass (3 steps)`), and a trial that stopped short of the steps its task
declared is reported as incomplete rather than as a pass -- harbor records only the steps that ran, so
the declared count rides in the driver's state file as `step_count`. A flat trial resolves to the
trial-root paths it always has, and a stepped trial's Modal environment is found by the cleanup pass
rather than left standing.

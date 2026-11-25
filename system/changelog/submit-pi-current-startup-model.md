# A pi chat on OpenCode starts on a current model

A new pi chat signed into OpenCode Go or OpenCode Zen started on `kimi-k2.6`, a model
that has since been superseded. The workspace pinned nothing wrong: pi picks a startup
model from `defaultModelPerProvider`, a per-provider table compiled into its own
`dist/core/model-resolver.js`, and that table still names `kimi-k2.6` for `opencode`,
`opencode-go` and `openrouter`.

`seed_home_skeleton.sh` could not cover those providers. It seeds
`defaultProvider` / `defaultModel` (OpenRouter, `z-ai/glm-5.3`), but pi keeps exactly one
saved default and ignores it on any provider the user has no credential for -- on an
OpenCode account that default never matched, and pi fell through to its compiled table.

So the correction now lives where pi actually decides: `.pi/extensions/model_defaults.ts`,
a project extension pi loads into every agent whose working directory is this repo (the
same mechanism `policy_guards.ts` and `tk_workflow.ts` already use, so nothing new has to
be provisioned). At process startup it compares the model pi resolved against a small
table of deprecated startup models and, on a match, switches to the listed model on the
same provider. `opencode/kimi-k2.6`, `opencode-go/kimi-k2.6` and
`openrouter/moonshotai/kimi-k2.6` now start on `deepseek-v4.1-flash`.

The table is deliberately narrow, and staying on the same provider is the point:

* Only the models listed are replaced. A model the user picked -- saved with `Ctrl+S`, or
  restored from a resumed session -- is not in the table, so it is never touched.
* A replacement stays on the provider the chat is already authenticated against, so it
  can never need a credential the user has not supplied. If the replacement is missing
  from the catalog or its provider turns out to be unauthenticated, the handler does
  nothing and pi keeps what it chose.
* It runs at process startup only. A resumed session keeps its own model, and `/new`
  keeps the model you were already on.

Keeping it current is one line: each key is `provider/modelId` as pi's own table spells
it, each value is the model to start on instead. pi's table is the thing that goes stale,
so when a model in this one is itself superseded, edit that line -- no pi release and no
mngr release is involved.

Verified against pi 0.87.1 with an OpenCode Go credential. The session log records the
two-model startup (`opencode-go/kimi-k2.6` as pi resolved it, then
`opencode-go/deepseek-v4.1-flash`), every assistant message runs on the latter, and with
mngr's lifecycle extension loaded as it is in a chat, `model_state.json` -- what the
chat's model bar reads -- ends at `{"model":"opencode-go/deepseek-v4.1-flash",...}`.
`.pi/extensions/pi_extensions_test.py` covers the replacement, the untouched cases
(resumed session, `/new`, a model the user picked), the two fallbacks (replacement absent
from the catalog, provider unauthenticated), and a model registry that throws.

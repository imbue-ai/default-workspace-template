# Model bar — dynamic per-account catalog (the "third picker") — DEFERRED task

Status: DESIGNED, DEFERRED (2026-08-11). Separate from the message-lifecycle wiring.

## Settled scoping (two orthogonal axes — do NOT conflate)

- **`PickerMode`** — how the MODEL picker *renders its list*. Scoped to the model dropdown only.
  Three types: `LIST` (static rows), `SEARCH` (fetch+filter, huge lists), and the new
  **`DYNAMIC`** (populate-at-show from `model/list`). This is the "third picker."
- **`SwitchMode`** — how a pick is *applied + reconciled*, across ALL three axes (model / effort /
  fast). Two types: `EAGER_THEN_RECONCILE` (chip moves optimistically on click, snaps to disk) and
  `ON_CHANGE` (chip moves ONLY when the backend confirms). Codex = **`ON_CHANGE`**.

They are independent: PickerMode is "how the model list looks," SwitchMode is "what happens when you
click any of the three chips."

## Fast-availability — SETTLED decision (no special handling)

Keep the Fast chip always visible. Clicking it issues `thread/settings/update{serviceTier:"priority"}`.
Because codex is `SwitchMode.ON_CHANGE`, the chip moves ONLY if the daemon actually applies it — on
an account without the fast tier, nothing moves (benign no-op), and `thread/settings/update` degrades
via warning+fallback rather than erroring. So we do NOT need the dynamic catalog to *hide* Fast, and
we do NOT duplicate anything. The one enabler is `SwitchMode.ON_CHANGE` for codex (so the frontend
never optimistically moves the chip). The dynamic catalog below is now pure OFFERING POLISH (show the
account's real models/efforts, and only show Fast where the account has it) — nice-to-have, deferred.

## Re-add `SwitchMode.ON_CHANGE` (part of THIS deferred track)

`ON_CHANGE` was removed in commit `01f71920ba` ("remove dead switch-mode machinery") — it was NOT
dead, it's the mode codex wants. Today `SwitchMode` has only `EAGER_THEN_RECONCILE` (`model.py:128`).
The switch itself is already instant regardless of mode (`switch()` fires `thread/settings/update`,
never `/model` keystrokes). `ON_CHANGE` only changes the CHIP-MOVE policy: move-on-confirm instead of
move-optimistically-then-snap-back. Its ONLY functional payoff is killing the one-frame flicker when
Fast is clicked on an account without the fast tier (EAGER moves the chip "on," reconcile snaps it
back). Purely cosmetic — deferred here with the rest of the fast-chip polish.

Scope of the re-add (~10 lines): add `ON_CHANGE = "on_change"` to `SwitchMode`; set codex's harness
spec to it; in the frontend chip-move path, skip the optimistic move when the harness is `ON_CHANGE`
(move only on the `minds_model_state.json` reconcile). Do this AFTER the ledger wiring lands (it
touches `codex/model.py`), not concurrently.

## Problem

The model bar shows `[Model][Effort][Fast]`. Today codex uses a STATIC hand-written `CODEX_CATALOG`
(`harnesses/codex/model.py`) with 5 models, each hardcoded `supports_fast=True` and a fixed effort
set. That is wrong per account:
- An account without the priority (fast) tier still sees the Fast button.
- An account whose models/efforts differ from the hardcoded 5 sees the wrong list.

Effort is ALREADY per-model (the picker shows a model's declared `efforts`). We want **fast
presence to be per-model + per-account the same way**, and the model list itself to be the
account's real one — all from one source, no catalog duplication (fast is a per-model axis, not a
separate model).

## The source: `model/list` is per-account and per-model

`client.model_list()` returns `data: [Model]`, each `Model` carrying:
- `supportedReasoningEfforts: [{reasoningEffort, description}]` + `defaultReasoningEffort`
- `serviceTiers: [{id, name, description}]` + `defaultServiceTier`
- `displayName`, `description`, `hidden`, `isDefault`, `model` (the slug).

This is the ACCOUNT's real models with the ACCOUNT's real tiers. So:
- **effort dropdown** = the model's `supportedReasoningEfforts` (already the pattern).
- **`supports_fast`** = the model's `serviceTiers` includes the priority/fast tier
  (`fast <-> serviceTier == "priority"`). No fast tier for that model/account -> no Fast button.
- **which models to offer** = `data` (filter `hidden`), per account.

One catalog, correct per account. No fast/non-fast duplicate entries.

## The build (three parts)

1. **Backend — build `ModelOptions` from `model/list`, not the static catalog.** At agent init
   and/or picker open, call `client.model_list()` and construct the codex `HarnessCatalog.options`
   dynamically: per-model `efforts` from `supportedReasoningEfforts`, `supports_fast` from
   `serviceTiers` (has the fast tier), plus `displayName`/`hidden`/`isDefault`. Keep the hand
   catalog only as optional label enrichment. Decide the exact fast-tier id (`priority` vs the
   model's `defaultServiceTier`).

2. **A third `PickerMode` — `DYNAMIC` (populate at show).** Today: `PickerMode.LIST` (static rows,
   claude/codex) and `PickerMode.SEARCH` (fetch account set on open, filter+cap, pi). Add a third:
   a normal-sized list POPULATED AT SHOW TIME from `model/list` (copy the searchable
   fetch-on-open structure but render as a normal list; search optional above a threshold). The
   offer endpoint (`GET /api/agents/<id>/model-options`) must return the full per-model data
   (efforts + fast), not just a set of ids, so the picker shows the right effort/fast per model.

3. **Frontend (`views/ModelBar.ts`).** Render the dynamically-populated list; the effort dropdown
   and the Fast toggle come from the SELECTED model's fetched data (fast hidden/disabled when that
   model has no fast tier). No change to the read/switch contract: the chip stays backend-driven
   (A2), moving only on `thread/settings/updated`.

## Why it is safe even before this lands (the current static-catalog gap)

- The chip is backend-driven: it can only show "fast on" if the daemon actually applied it
  (`thread/settings/updated`), so a wrongly-offered Fast click can't lie.
- `thread/settings/update{serviceTier:"priority"}` on an account without it degrades benignly
  (warning + fallback), not an error — a no-op, not a crash.

So the static catalog's hardcoded `supports_fast=True` is a cosmetic/offering bug (shows a button
that no-ops), not a correctness hazard. This task fixes the offering to match the account.

## Relation to the fast-mode track

`fast <-> serviceTier == "priority"` is the same mapping the `[first]`-template fast-mode intent
and the keep-fast-mode prompt use (see `../first-agent-and-popups-plan.md`). This dynamic catalog
is the picker/offering side; that track is the default/prompt side. They share the tier mapping.

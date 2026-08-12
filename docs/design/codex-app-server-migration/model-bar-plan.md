# Codex model bar: dynamic, app-server-driven (plan)

Status: PLAN. Goal: wire codex's model bar to the app-server so (1) switching is instant via a new
**ON_CHANGE** mode (no optimistic overlay), and (2) the picker is **fully dynamic** — the model list,
its per-model efforts, and fast support all come from the daemon's `model/list` (subscription-tier
dependent), with **no static catalog at all**. Every claim below is confirmed by reading the code +
live probes against a real codex 0.147 daemon.

Convention: the shared model spine is `harnesses/model.py`; codex's model code is
`harnesses/codex/model.py`; the frontend bar is `frontend/src/views/ModelBar.ts` +
`models/ModelSettings.ts` + `models/HarnessCatalog.ts`.

---

## 0. What already exists — do NOT rebuild

- **`client.model_list(include_hidden=False)`** and the `CodexModel` type already exist
  (`mngr_codex/app_server_client.py`): `id`, `display_name`, `hidden`, `is_default`,
  `default_reasoning_effort`, `supported_reasoning_efforts[]`, `service_tiers[]`. This is the whole
  dynamic-catalog source — reachable today.
- **`client.settings_update(model=, effort=, service_tier=)`** → `thread/settings/update`. Codex's
  `switch()` already uses it (fast, app-server, not a slash command).
- **The ledger already writes `minds_model_state.json`** on `thread/settings/updated`
  (`ledger._on_settings_updated` → `{model, effort, fast}`, `fast = serviceTier == "priority"`). The
  "codex writes its state to the shared file" abstraction is DONE.
- **The frontend already derives ON_CHANGE for free**: `ModelBar` computes
  `optimistic = switch_mode === "eager_then_reconcile"`, so a new `"on_change"` mode → `optimistic
  = false` automatically (interactive, chip waits for the pushed choice). No overlay change needed.
- **The fast slot already gates on `matched.supports_fast`** — no fast toggle renders when the model
  doesn't support fast. Exactly the desired behavior; it just needs `supports_fast` sourced from
  `model/list`.
- **The dynamic-offer plumbing exists**: `resolver.list_offered_models()` served via
  `GET /api/agents/{id}/model-options`, fetched per picker-open. Today it returns only ids and is
  consulted only for a *search* picker.

---

## 1. Live-verified facts (the empirical basis)

Driven against a real daemon (`model/list` + real `thread/settings/update` switches):

- **`model/list` is per-account and per-model.** 5 visible + 3 hidden here; efforts differ per model
  (Sol/Terra `low→ultra`, Luna `low→max`, 5.5/5.2 `low→xhigh`); `service_tiers` = `['priority']` on
  the 5.6 family + 5.5, `[]` on 5.2. A static uniform catalog cannot represent this.
- **The switch loop is ~170ms** (`settings/update` RPC → `thread/settings/updated` echo → file
  write); first switch after connect ~1s (daemon warmup). Fast enough that ON_CHANGE (chip moves on
  the confirmed change) feels instant.
- **`thread/settings/updated` carries the full effective settings** `{model, effort, serviceTier,
  ...}`; the ledger writes the file from it. `serviceTier: "priority"` → `fast: true`;
  `None`/`"default"` → `fast: false`.
- **A no-op switch (re-set the current value) emits NO `settings/updated`** — so the switch endpoint
  must force one authoritative `ModelChoice` broadcast (it already does: `refresh_model_choice`).
- **The daemon does NOT enforce a model's `service_tiers`.** Switching to gpt-5.2 (no `priority`) with
  `serviceTier` still `priority` KEPT `priority` (file stayed `fast: true`). So fast-clearing on a
  no-priority model is **frontend-enforced** (§4), not daemon-enforced.

---

## 2. The architecture — a per-agent dynamic catalog (no static list)

Today codex's "catalog" is the static `CODEX_CATALOG` (hand-written options), matched against by both
the chip (`_recompute_model_choice`) and the picker. **We delete the static options entirely** and
make codex's catalog **per-agent, sourced from `model/list`.** Three consumers need the per-agent
model set:

1. **The picker** (`GET /api/agents/{id}/model-options`) — the offer set, on open.
2. **The chip** (`_recompute_model_choice`) — to match the live `{model, effort, fast}` from the file
   to a display option (label + which slots show).
3. **The switch endpoint** — to validate/clamp the requested selection.

**Where the model set lives: cache it on the live connection.** `CodexLiveConnection` already holds a
per-agent daemon connection; it fetches `client.model_list()` once on connect and caches the
resulting `tuple[CodexModel, ...]`. `AgentManager` exposes it (like it exposes the ledger). The chip
recompute reads the cache (no daemon call in the hot path); the model-options endpoint reads the cache
(and MAY re-fetch on open for freshness — tier changes are rare, so cache-on-connect is the default).

**`CodexModel` → `ModelOption` mapping** (one pure function, used by all three consumers):
- `id` = `model` (the switch id); `label` = `display_name`;
- `efforts` = `supported_reasoning_efforts` → `EffortChoice(level=...)` (per-model, verbatim);
- `supports_fast` = `"priority" in service_tiers`;
- `in_picker` = `not hidden`;
- (optionally surface `is_default`.)

So the codex catalog options are now computed per-agent from the daemon. The static harness-level bits
stay (they are config, not a model list).

---

## 3. The changes (concrete)

### Backend
1. **`SwitchMode.ON_CHANGE`** (new value in `harnesses/model.py`). Codex's harness catalog uses it.
   Docstring: the switch is a fast app-server request, so the chip moves on the confirmed change, not
   optimistically.
2. **A dynamic `PickerMode`** (e.g. `PickerMode.DYNAMIC`) OR reuse LIST + a per-agent options source.
   Recommendation: `DYNAMIC` — a LIST-rendered picker whose *options are per-agent* (from
   model-options), distinct from the static-catalog LIST. (Decision D1 below.)
3. **Codex harness catalog** (`CODEX_CATALOG` → a factory): `options=()` (empty — dynamic),
   `switch_mode=ON_CHANGE`, `picker_mode=DYNAMIC`, `powered_by_label="Codex"`,
   `native_atomic_shoulder_tap_possible=True`. Delete the hand-written model options + efforts.
4. **`CodexLiveConnection`**: fetch + cache `model_list()` on connect; expose it. `AgentManager` grows
   a `get_codex_models(agent_id) -> tuple[CodexModel, ...] | None`.
5. **`CodexModelResolver`**: override `list_offered_models()` — and, since ids alone lose the per-model
   efforts/tiers, the **model-options endpoint must return full `ModelOption`s** for a dynamic harness,
   not just ids (extend `ModelOptionsResponse` to carry full options when the harness is dynamic; the
   resolver builds them from the cached `model_list`). `switch()` is unchanged.
6. **`_recompute_model_choice`**: for a dynamic harness, match the file identity against the agent's
   per-agent options (from the cached `model_list`) instead of the static catalog. This is the one
   spine change — the match source becomes per-agent for codex. (Everything else — read the file,
   `match_option`, broadcast — is unchanged.)
7. **`_set_model_choice_endpoint`**: validate against the per-agent options (not the static catalog).
   Keep the forced broadcast (`refresh_model_choice`) so a no-op switch still reconciles.

### Frontend
8. **`ModelBar`**: for a dynamic `picker_mode`, source the model options **per-agent** (fetch full
   options from `/model-options` on open, like the search picker fetches its offer set) instead of
   `catalog.options`. The chip's `matched` still comes from the backend `model_choice` (computed
   against the per-agent set), so the label + which slots show are already correct. ON_CHANGE needs no
   change (the `optimistic` derivation already yields `false`).
9. **`HarnessCatalog.ts` / `ModelOptionsResponse`**: extend the model-options wire shape to carry full
   `CatalogModelOption[]` (id, label, efforts, supports_fast, in_picker) for a dynamic harness.

### Delete
10. The static `CODEX_CATALOG` model options + `_CODEX_EFFORTS` (no static list, per the directive).

---

## 4. The fast/tier rule is FRONTEND-ENFORCED (do not skip)

The daemon keeps whatever `serviceTier` is set even on a model that doesn't support `priority`. So the
guarantee "a no-fast model has no fast, and clearing it works" lives in the frontend:
- `supports_fast` MUST come from `model/list` `service_tiers` (per model). The fast slot renders iff
  `matched.supports_fast` — already the code.
- The model-pick clamp `fast: option.supports_fast ? currentFast : false` MUST send the fast axis on a
  model switch to a no-priority model (→ `service_tier=None` → the daemon clears `priority`). Already
  the code — but now load-bearing, so it needs a test with real `model/list` tiers.
- If the account has no `priority` tier anywhere, no model lists it → `supports_fast=false` everywhere
  → the fast slot never shows and the choice can't carry fast. This is exactly the desired behavior,
  and it falls out for free once `supports_fast` is daemon-sourced.
- **Write all three axes on a model switch (codex).** Because the daemon does not enforce tiers, a
  model-axis change must send `model` + `effort` + `fast` together (not only the diffed axes): the new
  model's effort lands valid and `service_tier` is explicitly set/cleared for its fast support, so a
  stale `priority` can never survive a model change. (The per-axis `changedAxes` clamp does most of this
  already; codex makes it unconditional whenever the model axis changes.)

---

## 4b. Selected vs. effective model — framework fallbacks (the chip can currently LIE)

`minds_model_state.json` is written from `thread/settings/updated` — the thread's **selected** settings
(what the user picked; also seeded on connect from the `thread/resume` response's `model`/`effort`/
`serviceTier`). But the daemon can run a turn on a **different effective model** than the thread
setting: a per-turn override — a framework fallback when the account is over quota / on a lower tier, or
the docs' resume-time "one-time model-switch instruction." Such an override IS recorded in the rollout
per turn (`turn_context.model` + `reasoning_effort` — present in the rollout fixture), but it does NOT
necessarily emit `thread/settings/updated`, so today it would **not** reach the chip — the bar would
show the *selected* model, not the one codex actually ran. (The session parser even carries a
per-message `model` field but leaves it `_UNKNOWN_MODEL`, unextracted.)

**Fix: source the effective model from the rollout, not only the selected settings.** The codex watcher
extracts each committed turn's `turn_context.model` / `reasoning_effort`; when it differs from the
selected settings, the bar reflects the **effective** model (the truth of what ran). Simplest: on each
turn, write the effective per-turn model into the model state so the chip follows reality; alternative:
keep selected + effective separate and mark the chip when they diverge. The bar's job is to show what is
actually running, so reflecting the effective model is the right default.

**Verification limit (be honest):** I could not induce a real quota fallback, so whether such a fallback
*also* emits `thread/settings/updated` is unconfirmed. Sourcing the effective model from the rollout
`turn_context` is robust either way — it reflects the truth regardless of whether the daemon mutates
thread settings. Verify against a real fallback if one can be induced (or by inspecting the rollout
after a tier downgrade).

## 5. Preserve the shared abstraction

No change to the spine's shape: every harness still writes `minds_model_state.json`, the path-watch
still fires `_recompute_model_choice`, which still reads the file and broadcasts a `ModelChoice`. The
only thing that becomes per-agent (for codex) is the *option set the identity matches against*. Claude
and pi are untouched. The model bar stays "backend owns the choice; the frontend renders it."

---

## 6. Open decisions + sequencing

**Decisions:**
- **D1 — picker mode**: a new `PickerMode.DYNAMIC` (per-agent options, LIST-rendered) vs. reuse LIST
  with a per-agent options source. Recommend `DYNAMIC` for a clear, typed signal that options are
  per-agent, live.
- **D2 — model_list freshness**: RESOLVED — re-fetch `model/list` on every picker-open (always fresh; a
  fast RPC). The per-agent set for the chip-match (§2) is seeded on connect and refreshed by each open's
  fetch.
- **D4 — selected vs. effective model (§4b)**: RESOLVED in principle — reflect the EFFECTIVE per-turn
  model (from the rollout `turn_context`) so a framework fallback shows in the bar, not just the selected
  settings. Exact surfacing (overwrite the state file vs. a separate effective signal) is an
  implementation choice; verify the fallback signal against a real downgrade.
- **D3 — hidden models**: `model/list` `hidden` → `in_picker=false` (matchable if the live state
  reports it, never offered), mirroring claude's `ultra`. Recommend yes.

**Sequencing:**
1. Backend: `SwitchMode.ON_CHANGE` + the `CodexModel→ModelOption` mapper + cache `model_list` on the
   live connection + expose it.
2. Backend: model-options returns full per-agent options; `_recompute_model_choice` + the switch
   endpoint match/validate against them; codex catalog goes options-empty + ON_CHANGE + DYNAMIC.
3. Frontend: dynamic picker sources per-agent options; extend the model-options wire shape.
4. Delete the static `CODEX_CATALOG` options. Tests: per-agent match, ON_CHANGE (no overlay), the
   fast-clamp with real tiers, hidden handling.

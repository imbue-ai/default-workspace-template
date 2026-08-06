# Model bar — design & build plan

The composer's model bar renders `[Logo][Model][Effort][Fast][file][send]`. It shows
which model/effort/fast an agent is on and lets the user change it. Everything it
shows is **data**, derived from a per-harness catalog plus a per-agent live choice —
the frontend holds zero hardcoded model knowledge (no model names, no effort lists,
no logo paths). Adding a harness is one `harnesses/<h>/model.py` + one registry field.

This mirrors the existing `activity_state` machinery (`agent_manager.py`), which is the
proven template throughout: a per-agent value, recomputed on a narrow trigger, cached,
serialized into `get_agents_serialized()`, broadcast on the agents WebSocket, consumed
by the frontend agents store.

---

## 0. Two data sources, strictly separated

- **Catalog** (static, per-harness, compile-time): which models exist, labels, which
  efforts each supports, which are *shown* vs merely *valid*, whether each model
  supports fast, whether the harness can switch, and the logo SVG. Identical for every
  agent of a harness. Delivered once via `GET /api/harnesses`.
- **Choice** (live, per-agent, runtime): which `(model, effort, fast)` this one agent is
  currently on, plus a provenance. Delivered per-agent on the agents WebSocket as
  `model_choice`, exactly as `activity_state` rides there today.

The chip renders slots from catalog + choice. Nothing else feeds it.

Provenance (`ModelChoiceSource`): `guess` (from launch config, pre-first-turn) or
`live` (read from disk after the harness wrote real state). The frontend adds a
third, `pending`, purely locally for an optimistic pick; the backend never emits it.

---

## 1. File structure (commonized; Claude is not special)

Today's `claude/model_settings.py` + `claude/fast_mode.py` are the wart: Claude-specific
files sitting where they read as general. Everything model-related becomes a shared
spine plus one identical-shaped `model.py` per harness.

```
harnesses/
  registry.py          HarnessSpec (+ model_spec field) + HARNESS_SPECS   ← one entry per harness
  session_watcher.py   AgentSessionWatcher ABC        (exists)
  activity.py          HarnessActivityTracker ABC      (exists)
  model.py             NEW — shared model spine:
                         EffortLevel, EffortChoice, ModelOption,
                         ModelIdentity, ModelChoice, ModelChoiceSource,
                         HarnessModelSpec, HarnessModelResolver (ABC),
                         SwitchResult, match_option()  ← shared matcher (§4)
  path_watch.py        NEW — shared "watch these paths → call this cb" util,
                         thin wrapper over watcher_common.WakeOnChangeHandler
  events.py  harness_type.py  tool_labels.py           (exist)
  claude/
    model.py           NEW — ClaudeModelResolver + its catalog + the
                         settings.json read / fastMode layering guts
                         (ABSORBS today's model_settings.py + fast_mode.py per-agent parts)
    launch_defaults.py  the workspace fast-mode launch-default decision
                         (read/write_workspace_fast_mode_decision). STAYS under claude;
                         it is a launch-config concern, NOT part of the resolver.
    watcher.py  activity.py  activity_state.py  session_parser.py
    tool_labels.py  auth*.py  icon.svg
  codex/
    model.py           NEW — CodexModelResolver + its catalog + the
                         rollout thread_settings_applied read
    watcher.py  activity.py  activity_state.py  session_parser.py
    tool_labels.py  icon.svg
```

Every harness now has the same shape: `watcher.py` / `activity.py` / `model.py` + a
parser. Nothing at the `claude/` top level implies Claude is the only one with a picker.

**Note:** `launch_defaults.py` (the "run fast until the user answers" workspace prompt)
must NOT be imported by any resolver. It gates the *launch* default, not the per-agent
model. Keep it separate so it doesn't get swept into `claude/model.py`.

---

## 2. Shared types (`harnesses/model.py`)

```python
class EffortLevel(StrEnum):
    # The full universe — declared efforts may exceed what any harness shows.
    LOW = "low"; MEDIUM = "medium"; HIGH = "high"
    XHIGH = "xhigh"; MAX = "max"; ULTRA = "ultra"

class EffortChoice(FrozenModel):
    """One effort in a model's declared set."""
    level: EffortLevel
    in_picker: bool = True     # False = valid + matchable, but hidden from the dropdown

class ModelOption(FrozenModel):
    """One model in a harness's catalog. Static — never per-agent."""
    id: str                            # what switch() sends: "opus[1m]", "gpt-5.6-sol"
    label: str                         # human name: "Opus 5 (1M)", "GPT-5.6-Sol"
    efforts: tuple[EffortChoice, ...]  # DECLARED set = validity + matching universe;
                                       #   () means this harness has no effort axis
    supports_fast: bool                # per-MODEL, not per-harness
    in_picker: bool = True             # False = hidden model (matchable, not offered)

class ModelIdentity(FrozenModel):
    """The tuple that IS a selection. Resolvers return it; switch() sets it."""
    model_id: str
    effort: EffortLevel | None         # None only mid-merge; a resolved choice is concrete
    fast: bool

class ModelChoiceSource(StrEnum):
    GUESS = "guess"; LIVE = "live"     # backend emits only these; PENDING is frontend-only

class ModelChoice(FrozenModel):
    """The live, per-agent selection sent to the browser. The runtime half."""
    identity: ModelIdentity
    source: ModelChoiceSource

class SwitchResult(FrozenModel):
    ok: bool
    detail: str | None = None          # error surfaced by the endpoint on failure
```

### Subsetting (declared ⊇ shown), for efforts AND models

- **Matching / validation** uses the full declared set → a live-read `ultracode`/`ultra`
  or a hidden model still matches its option and displays.
- **The dropdown** renders only `in_picker` efforts / models → the UI shows the subset.

This is how "declare everything, show a subset" works uniformly. Claude declares
`ultra` (ultracode) with `in_picker=False`; codex declares `max`/`ultra` hidden and a
couple of hidden models (`gpt-5.4`, ...) matchable but not offered.

### The catalog + the registry wiring

Mode is **one value per harness**, and it governs all three axes (model / effort / fast)
uniformly. It has nothing to do with which axes are *shown* — that is decided purely by the
matched model's data (effort axis shown iff `efforts` is non-empty; fast axis shown iff
`supports_fast`). Visibility = data; interactivity = mode.

```python
class SwitchMode(StrEnum):
    EAGER_THEN_RECONCILE = "eager_then_reconcile"  # claude — optimistic: chip moves on click,
                                                   #   reconciles from disk after
    ON_CHANGE            = "on_change"             # switchable but NOT optimistic — chip updates
                                                   #   only once disk reflects the change
    READ_ONLY            = "read_only"            # codex v1 — display only, not interactive

class HarnessCatalog(FrozenModel):
    """The serializable, per-harness static half. IS the /api/harnesses wire shape."""
    options: tuple[ModelOption, ...]     # the catalog, display order
    default_model_id: str                # shown before config/disk says otherwise
    switch_mode: SwitchMode              # ONE mode; applies to model, effort, AND fast
    icon_svg: str                        # harness logo, currentColor monochrome

# resolver_class sits FLAT on HarnessSpec, beside its true peers watcher_class/tracker_class:
class HarnessSpec(FrozenModel):
    ...                                  # name, watcher_class, tracker_class, special_kinds
    resolver_class: type[HarnessModelResolver]   # AgentManager calls .build() on it
    catalog: HarnessCatalog                       # the serializable wire half
```

Adding a harness stays **one registry entry**. No parallel registry, no per-concern dict.
`READ_ONLY` renders each shown axis's current value with a non-interactive dropdown/toggle, so
codex's model/effort/fast cannot fire a switch by construction. Claude is `EAGER_THEN_RECONCILE`;
codex v1 is `READ_ONLY`; flip codex to a switchable mode once the one-shot `/model` patch lands.

---

## 3. `HarnessModelResolver` (per-agent, harness-specific; ABC)

The model analogue of `HarnessActivityTracker`. AgentManager owns one per tracked agent,
built from the agent's harness, and calls it instead of branching on the harness name.

```python
class HarnessModelResolver(ABC):
    @classmethod
    @abstractmethod
    def build(cls, agent_info: AgentInfo) -> "HarnessModelResolver":
        """Construct for one agent. Takes the whole AgentInfo (like the watcher) so each
        harness reads the paths IT needs; the caller never learns which."""

    @abstractmethod
    def guess_from_launch(self) -> ModelIdentity:
        """The launch-config selection. Reads the config FILE directly (claude:
        settings.json / managed overlay as written at launch; codex: config.toml).
        ALWAYS returns a fully concrete identity — effort = config value or the harness's
        declared default, never None — so the merged choice is never missing a field."""

    @abstractmethod
    def read_live(self) -> ModelIdentity | None:
        """The current on-disk selection, or None when disk has recorded nothing yet.
        Fields MAY be None individually (e.g. claude effort before the first /effort);
        the merge (§4) fills those from the guess. None (the whole return) means
        'nothing live yet, use the guess'."""

    @abstractmethod
    def watched_paths(self) -> tuple[Path, ...]:
        """Files/dirs whose change means read_live() may now differ. Drives the sole
        live recompute trigger (§5). A path that does not exist yet is fine."""

    @abstractmethod
    def switch(self, identity: ModelIdentity, send: Callable[[str], bool]) -> SwitchResult:
        """APPLY `identity`. The harness decides HOW: it may validate first (e.g. shell
        out to confirm the model exists), then send one or many pane commands via `send`
        (injected by the endpoint, bound to agent_manager.send_message_to_agent for this
        agent). Returns ok / an error. A display-only harness (ON_CHANGE) returns
        ok=False with a detail the endpoint maps to 409."""
```

### Resolution rule (in AgentManager, harness-blind) — per-field merge

Not a whole-identity `??`. Live wins per field; the always-concrete guess fills gaps:

```
model_id = live.model_id ?? guess.model_id
effort   = live.effort   ?? guess.effort      # claude effort unset in live → from guess
fast     = live.fast     ?? guess.fast
source   = LIVE if read_live() is not None else GUESS
```

So effort is never None by the time it reaches the chip — no default-label special-case,
no slot pop-in.

---

## 4. The matcher & the 🤷 rule (`match_option`, shared)

`match_option(identity, options) -> ModelOption | None`:

- Matches an option iff `base_alias(identity.model_id) == base_alias(option.id)` AND
  `identity.effort` is in that option's declared efforts (any `in_picker`) AND
  `identity.fast` is not True for a `supports_fast=False` option.
- Frontend behavior:
  - **Match** → render `[Logo][Model][Effort][Fast]` normally. Dropdowns show only
    `in_picker` models/efforts; Effort slot present only if the option has efforts;
    Fast slot present only if `option.supports_fast`.
  - **No match** (open model jammed into Claude Code, off-menu combo) → render a single
    **🤷** in place of all three slots. No name, no effort, no fast.

`match_option` lives once in `model.py`. The backend computes it on every `ModelChoice`
(guess and live alike) and attaches the resolved `ModelOption` (or null → 🤷) to the pushed
choice, so the frontend never re-matches.

**All three slots derive cleanly from that one matched `ModelOption` — nothing else feeds them:**
- **Model**: label = `option.label`.
- **Effort**: shown iff `option.efforts` is non-empty (else no slot). Dropdown = `option.efforts`
  where `in_picker`; current value = `choice.identity.effort`.
- **Fast**: bolt shown iff `option.supports_fast` (else no bolt at all). Lit = `choice.identity.fast`.
- **No match** → 🤷, none of the three slots.

Visibility and population are 100% a function of the matched option + the choice's identity;
the `switch_mode` only decides whether the (already-shown) slots are interactive.

---

## 5. Runtime plumbing — `_recompute_model_choice` (mirrors `_recompute_activity_state`)

New AgentManager state, parallel to the activity trio:

```python
_model_resolver_by_agent: dict[str, HarnessModelResolver]
_model_choice_by_agent: dict[str, ModelChoice]
```

Resolver built once in `_ensure_activity_tracking` alongside the tracker (the single spot
a harness name selects behavior, `agent_manager.py:1132`):

```python
if agent_id not in self._model_resolver_by_agent:
    self._model_resolver_by_agent[agent_id] = \
        get_harness_spec(harness).model_spec.resolver_class.build(agent_info)
```

Recompute (near-copy of `_recompute_activity_state:1163`):

```python
def _recompute_model_choice(self, agent_id, *, broadcast_on_change):
    resolver = self._model_resolver_by_agent.get(agent_id)   # under lock
    if resolver is None: return
    live = resolver.read_live()                              # disk read, OUTSIDE lock
    guess = resolver.guess_from_launch()
    identity = merge_per_field(live, guess)                  # §3 rule
    choice = ModelChoice(identity=identity, source=LIVE if live else GUESS)
    # under lock: if choice == cached AND _agents entry carries it → return (no-op guard);
    #   else cache it, rebuild _agents[agent_id] AgentStateItem with model_choice=choice
    if broadcast_on_change:
        self._broadcaster.broadcast_agents_updated(self.get_agents_serialized())
```

### Triggers — exactly two, each narrow

1. **New agent created / discovered** → `_recompute_model_choice(id, broadcast=False)` right
   where `_ensure_activity_tracking` already calls the activity recompute (`:1136`), so a
   new agent shows its GUESS immediately. The `AGENTS_FULL_STATE` rebuild reapplies the
   cached choice (same cached-state-reapply concern as activity, `:977`).
2. **The harness's own watched source changed** → `_recompute_model_choice(id, broadcast=True)`
   emits the LIVE value. Driven by a per-agent `path_watch` observer over `watched_paths()`.

**No** recompute off the transcript event stream. Model state is not routed through UI
events; each harness watches only the file/db it needs, and recompute fires only for that.
The no-op guard suppresses redundant broadcasts.

### Serialization — where it enters the WS payload

`AgentStateItem` (`models.py:136`) gains `model_choice: ModelChoice | None = None`, twin of
`activity_state`. `get_agents_serialized()` (`:539`) adds:

```python
"model_choice": a.model_choice.model_dump() if a.model_choice else None,
```

Rides the same `broadcast_agents_updated` → `AGENTS_FULL_STATE`/`AGENT_STATE` path. No new
socket, no new endpoint for the live value.

---

## 6. Switch path — `POST /api/agents/<id>/model` → `resolver.switch()`

One endpoint, one request shape covering all three axes. It does NOT build a command
string; it calls the resolver's `switch()`, so each harness decides what to do (validate,
send one line, send several, run a CLI check first).

```python
class SetModelChoiceRequest(FrozenModel):
    model_id: str
    effort: EffortLevel | None = None
    fast: bool = False

def _set_model_choice_endpoint(agent_id):
    agent_info = find(agent_id)                          # 404
    req = SetModelChoiceRequest.validate(...)
    resolver = agent_manager.model_resolver_for(agent_id)
    # validate against the harness catalog: model_id ∈ options (by base_alias),
    #   effort ∈ chosen option's declared efforts, fast ⇒ option.supports_fast → 400
    identity = ModelIdentity(model_id=req.model_id, effort=req.effort, fast=req.fast)
    send = lambda line: agent_manager.send_message_to_agent(AgentId(agent_info.id), line)
    result = resolver.switch(identity, send)             # 409 if display-only
    return 200 if result.ok else (500, result.detail)
```

Per-harness `switch()`:

- **Claude**: three separate sends — `/model <id>`, `/effort <effort>`, `/fast on|off`
  (they are distinct commands). Also records fast into the agent's launch settings (today's
  `write_fast_mode_setting` side effect) so it survives restart.
- **Codex**: `/model <id> <effort>` (single one-shot, applies + persists to config.toml),
  then `/fast on|off`. Fast is exposed for codex even though the toggle may no-op depending
  on account — accepted.
- **pi/opencode (later)**: may run a validity check before sending. Endpoint never knows.

The old `_set_fast_mode_endpoint` / `/fast` route folds into this (a `fast` field). The
`GET /model-settings` endpoint and `ModelSettingsResponse` are deleted (live value is
pushed, not pulled). `/api/workspace/fast-mode` (launch default) is unrelated and stays.

---

## 7. Static catalog endpoint — `GET /api/harnesses`

Serves the compile-time half for every harness at once; cacheable. Codex gated by
`FEATURE_FLAG_ENABLE_CODEX` (a disabled harness simply isn't in the map).

```python
def _get_harnesses_endpoint():
    return {
      harness.value: {
        "options": [opt.model_dump() for opt in spec.model_spec.options],
        "default_model_id": spec.model_spec.default_model_id,
        "switch_mode": spec.model_spec.switch_mode.value,
        "icon_svg": spec.model_spec.icon_svg,
      }
      for harness, spec in HARNESS_SPECS.items()
    }
```

---

## 8. Catalogs

### Claude (`claude/model.py`)
```
opus[1m]  "Opus 5 (1M)"  efforts: low,med,high,xhigh,max (shown) + ultra (in_picker=False)  fast=True
fable     "Fable 5"       efforts: low,med,high,xhigh,max (shown) + ultra (hidden)            fast=False
sonnet    "Sonnet 5"      efforts: (same)                                                     fast=False
haiku     "Haiku 4.5"     efforts: (same)                                                     fast=False
default_model_id = "opus[1m]"; switch_mode = EAGER_THEN_RECONCILE
```
- `guess_from_launch`: settings.json / managed overlay at launch → model (default
  `opus[1m]`), fast (`FAST_MODE_BEFORE_DECISION` or workspace decision), effort (config
  `effortLevel` or declared default `medium`).
- `read_live`: settings.json → model (`read_model_from_settings`), fast
  (`resolve_agent_fast_mode` across the two layers), effort (`effortLevel`, may be None).
- `watched_paths`: settings.json + managed overlay.
- `switch`: three sends + record fast to launch settings.

### Codex (`codex/model.py`)
```
gpt-5.6-sol   "GPT-5.6-Sol"   efforts: low,med,high,xhigh (shown) + max,ultra (in_picker=False)  fast=True
gpt-5.6-terra "GPT-5.6-Terra" (same efforts)                                                       fast=True
gpt-5.6-luna  "GPT-5.6-Luna"  (same)                                                               fast=True
gpt-5.5       "GPT-5.5"       (same)                                                               fast=True
gpt-5.2       "GPT-5.2"       (same)                                                               fast=True
default_model_id = "gpt-5.6-sol"; switch_mode = EAGER_THEN_RECONCILE
```
- `guess_from_launch`: config.toml `[model, model_reasoning_effort]` if present, else
  default + declared default effort.
- `read_live`: parse the LAST `thread_settings_applied` from the live rollout (via the
  `<state_dir>/codex_transcript_path` marker the watcher already follows) →
  `{model, effort=reasoning_effort, fast=(service_tier=="priority")}`. None until the first
  turn. Reads the rollout INDEPENDENTLY of the session parser (two read-only cursors on the
  same file, by design — do not merge them, that recouples transcript tailing with model
  resolution).
- `watched_paths`: the marker path (rotates; re-read like the watcher).
- `switch`: `/model <id> <effort>` then `/fast on|off`.

Codex assumes the one-shot `/model <slug> <effort>` patch (upstream issue #32212, ~20-line
Rust change across `slash_command.rs` / `model_popups.rs` / `slash_dispatch.rs`).

CORRECTION (adversarial review H2): until that patch is confirmed present at runtime, the
send is **NOT** a harmless no-op. Typing `/model gpt-5.6-sol high` into today's codex TUI
opens the interactive picker **modal** (wedging the pane, waiting on keyboard selection) and
drops `gpt-5.6-sol high` as literal composer text that the next Enter would send as a prompt.
Codex also has no `/fast` slash command. Therefore **codex ships display-only in v1**:
`switch_mode = ON_CHANGE`, `switch()` sends nothing, and the chip reflects terminal-side
changes via `read_live`. The `switch()` seam and catalog are still built, gated behind a
runtime capability check, so enabling write is one flag flip once the patch lands. Filing the
PR is a separate, parallel task. See §14 for the full revision.

---

## 9. Frontend — where each piece enters (data-in map)

Two stores, matching the two sources.

### A. Catalog store (`models/HarnessCatalog.ts`, new)
- **In**: `GET /api/harnesses` once at startup. `Map<harness, {options, default_model_id,
  switch_mode, icon_svg}>`. TS mirror of `match_option`.
- **Logo**: `icon_svg` is the raw SVG string; the `[Logo]` slot is `m.trust(icon_svg)`. No
  per-harness frontend asset, no `claudeLogoIcon()` special-case — the logo is data.

### B. Live choice — rides the existing agents store
- **In**: `AgentState` (`AgentManager.ts:13`) gains
  `model_choice?: {identity:{model_id, effort, fast}, source} | null`, parallel to
  `activity_state?` right below it. Arrives on the same `AGENTS_FULL_STATE`/`AGENT_STATE`
  messages already parsed there. No new subscription, no fetch-on-switch.

### C. `renderModelBar` (rewrite of `renderModelControls`, `MessageInput.ts:118`)
Reads catalog + choice, writes through one setter:
1. `harness = agentState.harness`; `catalog = catalogStore.get(harness)`. Absent → render
   nothing (flag off / unknown), graceful.
2. `matched = match_option(choice.identity, catalog.options)`. No match → single **🤷**, done.
3. `[Logo]` ← `catalog.icon_svg`.
4. `[Model]` ← `matched.label`; dropdown = `catalog.options` where `in_picker`.
5. `[Effort]` ← shown iff `matched.efforts` non-empty; current = `choice.identity.effort`;
   dropdown = `matched.efforts` where `in_picker`.
6. `[Fast]` ← shown iff `matched.supports_fast`; lit iff `choice.identity.fast`.
7. Whole bar read-only when `catalog.switch_mode === "on_change"`.

### D. Optimistic-then-reconcile (kept; reconciles from push, not poll)
Keep the per-agent single-flight chain (`ModelSettings.ts:50`, `applyChainByAgent`) that
serializes rapid clicks in click order — that is why it exists. Change only the truth source:
- On pick: set a LOCAL optimistic `model_choice` with `source:"pending"`, redraw; enqueue
  `POST /api/agents/<id>/model` onto the chain.
- Reconcile: NOT a settle-read GET (deleted). Pending is cleared **per axis** by an incoming
  `source:"live"` value for that axis (see §14 H1/M4). A whole-identity supersede would flicker
  when Claude's three sends land one at a time, and a no-op switch would strand pending forever
  (the recompute no-op guard suppresses an equal-valued broadcast). Both are fixed in §14: the
  endpoint forces one authoritative broadcast after `switch()` (bypassing the no-op guard), the
  frontend clears pending on any matching-axis live even when the value is unchanged, and a
  client-side timeout reverts stuck pending to last-known live as a backstop.

---

## 10. Deletions / replacements

| Today | Becomes |
|---|---|
| `ModelSettingsResponse` + `GET /model-settings` | deleted — live value pushed on agents WS |
| `_set_fast_mode_endpoint`, `/fast` route | folded into `POST /model` (`fast` field) |
| `_set_model_endpoint` Claude-hardcoded `/model <id>` | resolver-routed `switch()` |
| `ModelSettings.ts` GET-poll + `fetchModelSettings` on switch | catalog store (once) + WS choice (push) |
| module-level `is_valid_model_id`/`MODEL_OPTIONS` imports in server | catalog/resolver validation |
| `ModelOption` in `models.py` (Claude, no efforts) | `harnesses/model.py` (neutral, +efforts subsetting) |
| `claude/model_settings.py` + per-agent parts of `claude/fast_mode.py` | `claude/model.py` (resolver guts) |
| `renderModelControls` (model + fast only) | `renderModelBar` (data-driven, +effort, +logo, 🤷) |

`fast_mode.py`'s workspace launch-default → `claude/launch_defaults.py` (or left in place);
NOT imported by the resolver.

---

## 11. Build order

- **Slice 1 — backend spine + Claude, end to end.** `harnesses/model.py`, `path_watch.py`,
  `HarnessModelSpec` on the registry, `ClaudeModelResolver` (absorbing model_settings /
  fast_mode per-agent parts), `_recompute_model_choice` + cache + serialization + the two
  triggers, generalized `POST /model`, `GET /harnesses`, delete `GET /model-settings`.
  Frontend: catalog store, `model_choice` on `AgentState`, `renderModelBar`, delete
  `fetchModelSettings`. Ship Claude fully data-driven.
- **Slice 2 — Codex.** `CodexModelResolver` (rollout `thread_settings_applied` read, config
  guess, `switch`), codex catalog + icon, flag-gated in `/harnesses`.
- **Slice 3 (parallel, separate repo) — the codex one-shot patch.** File the upstream PR;
  fork+pin only if we can't wait.
- **Later** — pi/opencode/agy `model.py` + icons; per-account codex catalog widening if the
  🤷 fallback bites.

## 12. Tests (mirror `activity_state_test.py` / `model_settings_test.py`)

- Resolver unit tests per harness against real fixtures (a real settings.json; a real
  rollout with `thread_settings_applied`): `guess_from_launch`, `read_live` (incl. partial
  effort=None), `switch` (command shape / validation).
- `match_option`: declared-but-hidden effort/model still matches; unmatched combo → None (🤷);
  fast-on for a no-fast option → no match.
- `_recompute_model_choice`: no-op guard suppresses redundant broadcasts; GUESS→LIVE handoff;
  per-field merge fills effort from guess.
- Endpoint: unknown model_id → 400, effort not in option → 400, display-only harness → 409,
  send failure → 500.
- Catalog endpoint: every harness has a `model_spec` (extend `test_every_harness_has_a_spec`).

## 13. Residual notes (decide during build, none change the shape)

- Codex account-only models (real list is per-account from `/models`) not in the bundled
  catalog fall through to 🤷 — consistent with the rule; widen catalog later if needed.
- Two read-only cursors on the codex rollout (watcher + resolver) — intentional; comment so
  nobody recouples them.

---

## 14. Adversarial review outcomes — plan revisions

Three adversarial reviewers (architecture/commonization, code-reuse, correctness) stress-tested
this doc against the live code. The backend recompute spine (mirror-of-activity) was validated.
The findings clustered at the **seams**: codex disk/watch, frontend matching, and resolver
lifecycle. Verdicts below — ACCEPTED changes amend the sections above; STOOD GROUND items are
deliberately unchanged with reasons.

### Ship-blockers (ACCEPTED)

**H1 — optimistic pending can stick forever.** `_recompute_model_choice` inherits the activity
no-op guard (`agent_manager.py:1194`): it does NOT broadcast when the derived value equals cache.
So a switch that succeeds-as-a-command but doesn't change the derived identity (codex fast toggle
that can't reach `priority`; a refused claude `/effort` with no other axis moving) produces no
`live` broadcast, and the optimistic `pending` is never superseded — the chip lies permanently.
Fix (three parts): (1) the `POST /model` endpoint forces one authoritative broadcast of the
resolved choice after `switch()` returns, via a `force=True` recompute that bypasses the no-op
guard for that agent; (2) the frontend clears a pending axis on any incoming `live` value for
that axis, even when unchanged; (3) a client-side pending timeout reverts to last-known `live`.

**H2 — codex `switch()` is harmful pre-patch, not a no-op.** (See the §8 correction.) Typing
`/model <slug> <effort>` into today's codex opens the picker modal and wedges the pane. So codex
ships **read-only in v1**: the chip shows codex's current model/effort/fast (read from disk, live
via `read_live`) but does NOT switch — `switch_mode = ON_CHANGE`, `switch()` sends nothing, the
dropdowns/toggle are non-interactive. The seam + catalog are still built so switching is one flag
flip once the one-shot `/model` patch lands. (Correction to an earlier reviewer note: codex DOES
have a `/fast` command — so when switching is enabled, fast works; it is simply unused in the
read-only v1.) This corrects the doc's earlier "harmless no-op" claim.

### Resolver lifecycle — one root cause behind M2 / L1 / create-GUESS (ACCEPTED)

The plan hung resolver construction inside `_ensure_activity_tracking`, which early-returns when
the local state dir is absent (`agent_manager.py:1125`). That single gate caused three bugs:
remote agents never get a resolver (null choice + present catalog → undefined render), a
brand-new agent whose observe event beats the state-dir creation shows an empty chip instead of
the promised GUESS, and there is no teardown. Fixes:
- **Build the resolver independent of `state_dir.exists()`.** `guess_from_launch` needs only the
  config dir (often host-shared, present immediately) and `read_live` already returns None on
  missing files, so GUESS is always available. Only the live *watch* needs the state dir.
- **M1 teardown:** `_stop_activity_tracking` (and `remove_agent`) must pop
  `_model_resolver_by_agent` + `_model_choice_by_agent` AND stop the per-agent `path_watch`
  observer — symmetric with the activity trio. The observer thread otherwise leaks per destroyed
  agent. (A reset-on-interrupt hook is NOT needed — the resolver is stateless w.r.t. the
  transcript and re-reads disk each recompute, so a restart self-corrects.)
- **M2 remote:** a `null` `model_choice` renders logo-only + read-only (no dropdowns/toggle), the
  frontend never calls the matcher on a null choice, and the bar is not interactive cross-host.

### Codex watch target — M1/H1 depend on it (ACCEPTED)

`watched_paths() -> (marker,)` is wrong: the `codex_transcript_path` marker is rewritten only at a
fresh root turn (mngr_codex `set_active_marker.sh`), so a between-turns terminal `/model` never
fires the recompute and the chip lags. Fix: the codex resolver watches the **sessions dir
recursively** — exactly the idiom `CodexSessionWatcher` already uses (`codex/watcher.py:186`) — and
re-reads the rotating marker to find the live rollout. Extract a shared
`resolve_active_rollout_path(state_dir)` (marker read + rotation) that both the watcher and the
resolver call, so the rotation-follow logic exists once. The blanket rule "no recompute off the
transcript" is relaxed to **per-harness**: correct for claude (settings.json is a separate file),
but codex's model state lives in the transcript, so codex legitimately watches the rollout dir.
The two read cursors stay separate (STOOD GROUND, below).

### Commonization / structure (ACCEPTED)

- **`AgentStateItem` update via `model_copy(update={...})`**, not field-by-field reconstruction.
  Today `_recompute_activity_state` rebuilds the frozen model listing every field (`:1197`); adding
  `model_choice` there and in the ~6 other construction sites + the `AGENTS_FULL_STATE` reapply
  (`:982`) means each recompute must remember every *other* per-agent field or blank it — N fields
  become N² preserve obligations. `model_copy(update=)` decouples them. Migrate the activity path
  to it too. This is the one genuinely-shared mechanical step worth commonizing; it scales to the
  further harnesses/fields coming.
- **Flatten `resolver_class` onto `HarnessSpec`** beside `watcher_class`/`tracker_class` (its true
  peers — AgentManager calls `.build()` on it exactly like the other two). Split the rest into a
  serializable **`HarnessCatalog`** (`options`, `default_model_id`, `switch_mode`, `icon_svg`) that
  IS the `/api/harnesses` wire shape via `model_dump()` — no endpoint-side field-picking, no drift.
  Rename away from "model_spec": the icon is harness branding, not model data. So `HarnessSpec`
  gains two flat fields: `resolver_class` and `catalog: HarnessCatalog`.
- **Backend serves the match result.** Attach the resolved `ModelOption` (or null → 🤷) to each
  emitted `ModelChoice`; the frontend renders pushed live/guess values straight from it, and the
  optimistic path renders the `ModelOption` the user just clicked **directly** (a pick is always a
  catalog option — no match needed). This **deletes the TS `match_option` mirror entirely** rather
  than dual-maintaining it. `base_alias` stays mirrored (established, accepted precedent).

### Code reuse (ACCEPTED)

- **`path_watch.py` builds on `watcher_common.WakeOnChangeHandler` + `POLL_INTERVAL_SECONDS`** (the
  only shared watch primitive today) and its contract distinguishes per-file-non-recursive (claude)
  from recursive-root (codex rotating rollout). The two existing watchers' hand-rolled
  Observer+thread+loop blocks are noted as **future adopters** — not migrated now (avoid a fourth
  watching idiom; don't over-consolidate working code).
- **Config reads go through `mngr.utils.file_utils.read_json_dict`** (missing/malformed → `{}`) for
  the model reads, instead of copying `model_settings.py`'s try/except ladder into each resolver.
  EXCEPTION: the fast-mode reads deliberately distinguish absent (silent, None) from corrupt
  (logged) for the two-layer merge (`fast_mode.py:78-95`) — those keep their bespoke read.
- **Deletion list completeness (§10 additions):** also delete `SetModelRequest`, `SetFastModeRequest`
  (superseded by `SetModelChoiceRequest`), and the now-dead tests `claude/model_settings_test.py`,
  `frontend/src/models/ModelSettings.test.ts`, `WorkspaceFastMode.test.ts` (GET-poll path). These
  are build breakers if missed. `enqueueApply`'s tail-drain (the settle-read) is rewritten, not kept.

### Validation & UX (ACCEPTED)

- **Partial-update semantics (M3):** the endpoint merges the request against the agent's *current
  resolved choice* — an omitted `effort`/`fast` means "keep current," not "unset." Validate the
  merged concrete identity. Add the reverse check: reject a non-None `effort` for a model whose
  option has `efforts=()`.
- **Per-axis pending (M4):** clear only the pending field whose live value arrived; keep the others
  pending. Prevents the medium→high flicker from claude's three sequential sends.
- **Effort clamp on model switch:** switching model clamps effort into the new model's declared set
  (fallback to that model's default). Moot for claude/codex (shared effort sets) but the rule is
  baked in now for pi/opencode/agy.
- **Small adds:** toast on non-2xx switch carrying `SwitchResult.detail`; disable the bar per-agent
  while a switch is in flight (prevents stacked codex modals / compounded stuck-pending).

### STOOD GROUND (rejected changes)

- **No generic `PerAgentDerived[T]` framework.** All three reviewers' abstraction probe converged:
  activity and model share ~5 mechanical lines but diverge on ~30 (mtime vs two-file read, lifecycle
  gate vs none, `observe` vs stateless, `reset` vs `switch`). A framework parameterized over
  read/compare/field would be net complexity UP to save 5 lines, and fights the registry's
  "one spec per harness" spine. With more harnesses coming, the win is a *flatter* per-harness
  resolver, not a deeper meta-abstraction. The shared mechanical step (`model_copy(update=)`) is
  adopted; the framework is not.
- **Resolver `build(agent_info)` keeps its arg** (reviewer proposed symmetry with the arg-less
  activity tracker). The resolver matches the *watcher* precedent (`build(agent_info, ...)`) — both
  own their paths and do their own I/O. The tracker is the deliberate exception (pure signal cache,
  marker as ClassVar, stat done by AgentManager). Forcing symmetry would touch working code for no
  gain.
- **Two separate read cursors on the codex rollout** (watcher for the transcript, resolver for
  `thread_settings_applied`). Only the *watch target* is fixed (above); the reads stay independent
  and read-only. Folding model-state into the transcript parser/event-stream would recouple two
  concerns we want separate as harnesses multiply. Deferred as a possible future, not done now.

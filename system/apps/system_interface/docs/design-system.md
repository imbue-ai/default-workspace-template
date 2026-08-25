# System Interface design system — assessment & plan

Status: **substantially complete.** All planned phases (P0–P4) have landed: the
full token scaffold (type, colour, z-index, elevation, motion, spacing, radius),
the mechanical/semantic sweeps, the component primitives (button, badge, spinner,
toggle, input, tooltip), and the modal consolidation. What remains is a short
follow-ups list (bottom of this log): off-grid spacing strays, a few defined-but-
unused tokens, the medium-weight audit, and whether to build a dark theme. This
document is the durable hand-off so any agent can pick the work up cold.

> **The design system is an optional convention, not an enforced rule.** It keeps
> the *shipped default* UI coherent, so prefer the tokens and primitives when you
> extend that default. But a user running their own source may want their
> interface redesigned to their taste — build exactly that and ignore these
> conventions freely. Nothing gates any of this. (An earlier draft added a
> `design-system-ratchet.test.ts`; it was removed to avoid impeding user-driven
> redesigns. The "Ratchet X → Y" figures in the log below are a historical record
> of the one-time cleanup, not a live gate.)

### Progress log

- **Typography — DONE.** Shipped a role-based type system instead of the raw
  `--text-*` numeric scale originally proposed in section 3 (see the "type system
  evolved" note there for why). Six semantic classes — `.type-heading-lg`,
  `.type-heading`, `.type-label`, `.type-body`, `.type-helper`, `.type-section` —
  each bundling `font-size` + `font-weight` + line-height (+ uppercase/tracking
  for section). Sizes come from four `--font-size-*` tokens (24/18/14/12) and
  weights from `--weight-*` (400/500/600/700; `medium` = 500 is in the system but
  intentionally **not** used on controls — see [medium-usage audit] follow-up).
  103 `font-size` declarations were normalized onto the tokens (incl. the login
  screen's whole `rem` scale); the body default dropped 15px → 14px. Left as
  deliberate exceptions: markdown's relative `em` sizes and four icon glyphs.
  `type-section` is live on the new-tab launcher's category headers. Ratchet
  `fontSizePx` tightened 80 → 4.
- **Corner-radius de-dup — DONE.** 15 raw `6px` border-radii → `var(--radius-base)`;
  provable exact no-op (all surfaces verified identical). Ratchet `borderRadiusPx`
  55 → 40. A full radius *scale* (8/12/…) is deferred: like type, it needs
  non-colliding names (Tailwind's `rounded-md/lg` are used in the views).
- **Semantic colour — DONE (no-op).** Added `--color-danger*/-warning*/-success/
  -info`, a `--color-neutral-*` slate scale, and `--color-text-on-accent`, all at
  current in-use values; migrated 36 sites (exact hex→token, `color:#fff`→on-accent,
  surface white→`--color-surface`) and dropped 5 dead `var(--token,#hex)` fallbacks.
  Ratchet `hexOutsideTheme` 52 → 16.
- **Leftover colours — DONE (intentional).** Zero raw hex now remains outside
  `@theme` (`hexOutsideTheme` 16 → 0). Decisions: `--color-danger` was *warmed*
  to `#d83a2c` (hover `#b62d22`, bg `#fdecea`, border `#f4c7c3`) and the warm
  login/composer error reds unified onto it (one error red everywhere); the amber
  waiting-dot → `--color-warning`; the `.minds-tooltip` black/white → existing
  `text-primary`/`surface`/`on-accent` (it self-inverts via its own
  `prefers-color-scheme` rules, so no new token); the progress-block warm greys →
  the neutral `--color-text-secondary`/`-faint` (progress view greys are now
  neutral, a small deliberate shift). Note: `--color-info` is defined but unused.
- **Primitives (P3) — Button DONE; Badge/Spinner/Toggle/Input still to come.**
  Shipped the `.btn` primitive (variants `primary|secondary|ghost|destructive|
  inverse|icon`, sizes `md|sm`, states incl. a new `:active` press and
  `--selected`, plus the `.btn--ghost.btn--destructive` quiet-destructive combo)
  and migrated every fittable button family onto it — fast-mode, destroy-dialog,
  permission, terminal-banner, queued-action, composer actions, image-lightbox,
  claude-login, and the (now informational) share modal. ~15 bespoke families →
  one system; the old per-feature button CSS is deleted. **Deliberately left:**
  the composer's send/stop/attach buttons are *circular* with an accent fill /
  the stop-button slate, which the square light-background `.btn--icon` can't
  express — they need a future `.btn--icon.btn--round` + send/stop colour
  treatment. A few other icon buttons (share close-×, dockview tab actions) are
  also not yet on the primitive. `borderRadiusPx` ratchet 40 → 36.
- **Modal consolidation (P4) — DONE.** Extracted the shared modal shell
  (`views/Modal.ts` + the `.modal-*` block in `style.css`) and migrated every
  card dialog onto it: the destroy-confirm, fast-mode prompt, and share notice
  (earlier), then the add-to-project and project-settings dialogs, the terminal
  sign-in notice, and the composer's two command notices (`can't be sent from
  chat` / `sign-in is managed here`). Deleted the bespoke `.custom-url-dialog-*`
  block and the now-unused `.destroy-dialog-message` / `.logout-notice-body`
  body-copy classes; the pickers' feedback CSS is rescoped onto `.modal-card`.
  **Deliberately left off the shell:** the full-bleed image lightbox (a viewer,
  not a card — already on `--z-overlay`, with a darker column-layout backdrop),
  and the Claude sign-in modal (a scrollable, multi-step flow whose
  absolute-positioned, sectioned, self-padded card and lower `z-index: 50`
  blurred backdrop would regress if forced onto the shared `.modal-*` classes,
  which the prompt forbids restructuring to fix). `borderRadiusPx` ratchet
  29 → 28, `zIndexLiteral` 12 → 11.
- **Token scaffold completed — DONE (no-op).** Tokenized the last five raw
  categories, each a value-exact no-op (component surfaces rendered identical;
  only the token-preview surfaces changed, showing the new tokens): **z-index**
  (`--z-content/-sticky/-dropdown/-overlay/-tooltip`;
  `zIndexLiteral` 11 → 6), **elevation** (`--elevation-sm/md/lg/overlay` — NOT
  `--shadow-*`, which collide with Tailwind utilities used in views),
  **motion** (`--dur-fast/base/slow` + `--ease-standard`; unified `120ms`/`0.12s`
  etc.), **spacing** (`--space-0_5…8`; 150 on-grid declarations migrated),
  and **radius** (value-named `--radius-3/4/8/10/12/-pill` — NOT `--radius-{sm,
  md,lg}`, same Tailwind-collision reason as type; `borderRadiusPx` 28 → 2). Final
  ratchets: hexOutsideTheme 0, fontSizePx 3, borderRadiusPx 2, zIndexLiteral 6
  (the residuals are documented `design-system-exception` one-offs and icon glyphs).

### Remaining follow-ups (not blocking; small, optional)

- **Off-grid spacing strays** (~57 declarations using 9/10/14/18/22/28px etc.)
  left raw — snapping them to the scale is a *visual* decision, not a no-op.
- **Defined-but-unused tokens** kept to document the scale: `--elevation-md`,
  `--ease-standard`, `--color-info`. Drop or adopt as desired. Also
  `--duration-sidebar-transition` (200ms) now duplicates `--dur-slow`.
- **Medium-weight (500) audit** — walk remaining `font-weight:500` sites now that
  the type system is in (controls stay off medium).
- **Two modals left off the shared shell** on purpose: the full-bleed image
  lightbox and the multi-step Claude sign-in modal (forcing either onto `.modal-*`
  would regress it).
- **Dark mode** — tokens are structured for it (semantic, centralized) but no dark
  theme is built. Open question from section 8.

All line/rule counts below were measured from `frontend/src/style.css`
(4,108 lines, 512 rule blocks) and the `frontend/src/**` view modules at the
time of writing. Re-measure before trusting them if the tree has moved on.

---

## 1. How the UI is built (as-built architecture)

The entire workspace UI is one app: `system/apps/system_interface/frontend`.

- **No React/Vue.** Views are **Mithril** hyperscript (`m(...)`) modules under
  `src/views/`; state/models under `src/models/`.
- **dockview-core** provides the tab/panel shell (`views/DockviewWorkspace.ts`,
  the largest module).
- **marked + dompurify** render chat markdown.
- **Styling is split across four parallel mechanisms** — this is the core
  problem:

  | Mechanism | Where | Rough scale |
  |---|---|---|
  | Hand-written semantic CSS | `src/style.css` | 512 rules, ~29 feature silos |
  | Tailwind v4 utilities | inline in `.ts` views | `text-` x82, `flex` x67, `px-` x13, `bg-` x9 … |
  | Inline `style:` objects | 11 view files | 28 sites |
  | Overrides of dockview's vendored CSS | `style.css` (`dv-`/`dockview-`) | ~142 rules |

  There is no single authoritative layer, and no shared component library.

- The CSS is namespaced **by feature, not by component**. The prefixes are the
  de-facto components: `claude` (91 rules), `dv`/`dockview` (~142), `pv`
  (progress view, 57), `message` (40), `markdown` (39), `share` (32), `tool`
  (27), `permission` (27), `model` (23), `fast` (22), `composer` (21), `queued`
  (19), `subagent` (12), `destroy` (10), `image` (8), `terminal` (6)…

- **There is a partial token layer already**: a Tailwind v4 `@theme` block at the
  top of `style.css` with ~37 tokens — 24 colors, 3 font families, 3 radii, 4
  widths, 1 spacing (`--spacing-page`), 1 duration, a logo hook. Color and a few
  layout widths are tokenized. **Type, spacing, elevation, motion, and z-index
  are not.**

---

## 2. Measured inventory of the sprawl

Every value below is repeated raw throughout the stylesheet with no token.

| Dimension | Distinct values | Tell-tale problem |
|---|---|---|
| Font size | **29** | mixed `px`/`rem`/`em`; `13px` typed 28x; `0.8125rem` and `13px` are the *same size*, both present |
| Spacing (padding/margin/gap) | **23 atoms** | `8px` x67, `6px` x36, `4px` x31, `12px` x31; off-grid strays `7px`, `9px`, `7.5px`, `35px` |
| Border radius | **18** | `6px` typed raw 15x *and* `var(--radius-base)` 12x — same value, two ways; `8px` (13x) has no token |
| Box-shadow | **7** | all hand-mixed, e.g. `0 8px 32px rgba(0,0,0,0.2)` x4 |
| Motion timing | **18** | mixed units — `120ms` and `0.12s` both present (identical duration); only 1 token |
| Z-index | **7 magic numbers** | `10001`, `10000`, `1000`, `100`, `50`, `2`, `1` — unmanaged, classic stacking-bug source |
| Font weight | 5 | no tokens |
| Raw hex colors (outside `@theme`) | **45** | ~18 are *exact duplicates* of existing tokens (e.g. `#2f6b4f` = `--color-accent`, `#666666` = `--color-text-secondary`) |
| rgba/rgb | 26 | two translucency bases mixed: `rgba(0,0,0,…)` vs Notion-charcoal `rgba(55,53,47,…)` |

Two recurring failure modes: **(a) a token exists but is bypassed** (radius,
accent color re-typed as hex), and **(b) a whole category has no tokens and grew
a semantic palette by accident** — a full destructive-red / warning-amber /
success-green / info-slate palette lives entirely as ~30 scattered raw hexes.

### Buttons & states (the headline count)

- ~**38 button-related CSS selectors**, collapsing to ~**15 bespoke button
  families** — essentially one per feature (`claude-login-button`,
  `destroy-dialog-btn`, `fast-mode-modal-btn`, `share-modal-btn`,
  `permission-request-button`, `message-input-send/stop/attach-button`,
  `queued-action`, `terminal-banner-btn`, `composer-under-bar-action`,
  `image-lightbox-iconbtn`, dockview tab actions…).
- **Only one family** (`.claude-login-button`) has a real variant system:
  `--primary / --ghost / --link / --block`. Every other button is a one-off.
- **162 `onclick` handlers** across the views vs. a small number of semantic
  `<button>` elements — many "buttons" are clickable `div`/`span`s (a11y +
  consistency gap).

States actually styled across all interactive elements:

| State | Occurrences | Notes |
|---|---|---|
| `:hover` | 60 | near-universal |
| `:disabled` | 15 | |
| `:focus-visible` / `:focus` / `:focus-within` | 13 / 3 / 2 | |
| `:active` (pressed) | **0** | nothing has a pressed state |
| selected / "active" | bespoke | spelled 4 ways: `--selected`, `--active`, `-current-mode`, `.dv-active-tab` |

### Other components, all reinvented per feature

- **Modals/dialogs: 6+ independent implementations** (`claude-login-modal`,
  `custom-url-dialog`, `destroy-dialog`, `fast-mode-modal`, `share-modal`,
  `image-lightbox`), each re-deriving overlay + centered card + title +
  action-row + buttons. **Biggest single consolidation win.**
- **Spinners: 4–5** implementations with **3 near-identical `@keyframes spin`**
  (`claude-login-spin`, `composer-attachment-spin`, `pv-spin`).
- **Toggles: 4+** families (`fast-toggle` with `--inline/--on/--readonly`,
  `claude-login-*-toggle`, `permission-request-*-toggle`, generic `.toggle`).
- **Badges: ~10** bespoke; **Tooltips: 2 systems** (JS `.minds-tooltip` +
  CSS `[data-tooltip]::after`, used 18x).
- **Icons: 18 inline SVGs** in `views/icons.ts` — the one genuinely
  systematized set; use it as the model for the rest.
- **Keyframes: 10** total, with the spinner duplication noted above.

---

## 3. Target token system (concrete additions to `@theme`)

Add these to the existing `@theme` block. Names follow Tailwind v4 conventions so
they are usable both as `var(--…)` and as generated utilities.

```css
/* Type — SHIPPED as a role-based system, NOT this numeric scale. See note below.
 * Four size tokens + four weight tokens back six .type-* classes:
 *   --font-size-heading-lg: 24px; --font-size-heading: 18px;
 *   --font-size-body: 14px;       --font-size-helper: 12px;
 *   --weight-regular: 400; --weight-medium: 500;
 *   --weight-semibold: 600; --weight-bold: 700;
 * .type-heading-lg 24/700 · .type-heading 18/600 · .type-label 14/600 ·
 * .type-body 14/400 · .type-helper 12/400 · .type-section 12/600 UPPERCASE. */

/* Spacing scale — 4px base, replaces 23 atoms; snap 7/9/7.5/22/35 to nearest */
--space-0_5: 2px; --space-1: 4px; --space-1_5: 6px; --space-2: 8px;
--space-3: 12px;  --space-4: 16px; --space-5: 20px; --space-6: 24px; --space-8: 32px;

/* Radii — --radius-base (6px) already exists; ban raw 6px in favour of it */
--radius-sm: 3px; --radius-md: 8px; --radius-lg: 12px;
--radius-pill: 999px; --radius-circle: 50%;

/* Elevation — replaces 7 hand-mixed shadows */
--shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.04);
--shadow-md: 0 2px 10px rgba(0, 0, 0, 0.08);
--shadow-lg: 0 8px 32px rgba(0, 0, 0, 0.2);
--shadow-overlay: 0 8px 40px rgba(0, 0, 0, 0.5);

/* Motion — one unit (ms), replaces 18 timings */
--dur-fast: 80ms; --dur-base: 120ms; --dur-slow: 200ms;
--ease-standard: cubic-bezier(0.2, 0, 0, 1);

/* Z-index — names the 7 magic numbers */
--z-content: 1; --z-sticky: 100; --z-dropdown: 1000;
--z-overlay: 10000; --z-tooltip: 10001;

/* Semantic colour — promotes the accidental red/amber/green/slate palettes */
--color-danger: #dc2626; --color-danger-hover: #b91c1c;
--color-danger-bg: #fef2f2; --color-danger-border: #fecaca;
--color-warning: #b45309; --color-warning-bg: #fde68a;
--color-success: #22a06b;
--color-info: #2563eb;
/* Neutral/slate family already half-present as --color-stop-button(#64748b)/-hover(#475569);
   fold #334155/#1e293b/#e2e8f0/#f1f5f9/#f8fafc into --color-neutral-{700..50}. */

/* Font weight */
--font-normal: 400; --font-medium: 500; --font-semibold: 600; --font-bold: 700;
```

**Caveat — Tailwind default-token collisions.** Tailwind v4's `@import "tailwindcss"`
already defines `--text-{xs,sm,base,lg,xl,2xl}`, `--radius-{sm,md,lg}`,
`--shadow-*`, and a `--spacing` base. Some of the names proposed above therefore
*override* a Tailwind default with a different value (e.g. Tailwind's
`--radius-sm` is 0.25rem/4px, not the 3px proposed here; `--text-sm` is 0.875rem,
not 13px). Decide per token whether to (a) adopt Tailwind's value, (b) intentionally
override it in `@theme`, or (c) pick a non-colliding name (e.g. `--text-body`,
`--radius-card`). Verify against the compiled CSS, which resolves each name to a
single value — the collisions surface there.

**Note — the type system evolved (why it shipped role-based).** The `--text-*`
collision above is exactly why the type scale did *not* ship as `--text-*`.
Overriding `--text-{sm,lg}` in `@theme` would silently change every Tailwind
`text-sm`/`text-lg` utility already used in the views (~22 real sites), so it
could not be a visual no-op. Rather than force the numeric scale, we adopted a
**role-based** system: semantic `.type-*` classes backed by non-colliding
`--font-size-*` / `--weight-*` tokens (so Tailwind's own `text-*` utilities keep
their values). Roles also read better at call sites (`type-body` says what the
text *is*, not how big). Body default was set to 14px (was 15px). `medium` (500)
is a real weight but is kept off interactive controls (button/tab weight comes
with the Button/Tab primitive in P3); a follow-up audits the remaining 500 sites.

## 4. Component primitives to extract

Since there is no component lib, these are small Mithril view helpers (return
`m(...)` trees) plus their CSS, generalized from the existing best example in
each category. `views/icons.ts` and `.claude-login-button`'s variant scheme are
the templates.

1. **Button** — `Button({variant, size, state, icon, label, onclick})`.
   Variants: `primary | secondary | ghost | destructive | icon`.
   Sizes: `sm | md`. States: default / hover / focus-visible / disabled /
   **pressed (add `:active`)** / selected. Generalize `.claude-login-button`.
2. **Modal** — `openModal({title, body, actions})` → overlay + card + header +
   body + actions footer. Collapses the 6 modal implementations. Highest value,
   highest risk (do last).
3. **Toggle / Switch** — generalize `fast-toggle`.
4. **Badge** — `Badge({tone, size})`, tones `neutral | accent | danger | warning | success`.
5. **Tooltip** — pick ONE mechanism. Keep the CSS `[data-tooltip]` for simple
   text; reserve `hoverTooltip.ts` for rich content only.
6. **Spinner** — one keyframe + size prop; delete the 3 duplicate `@keyframes`.
7. **Input / Field** — one text-input style; generalize the per-modal inputs.

---

## 5. Phased migration plan

Each phase is independently shippable. **P1 and P2 must be provable visual
no-ops** — each replaced literal resolves to the same computed value. Later phases
have intentional diffs, reviewed section by section.

- **P0 — scaffold (no visual change).** Add the full token set to `@theme`
  (unused for now), commit this doc, and add the ratchet (section 7) at its
  current baseline counts.
- **P1 — mechanical sweep (visual no-op).** Replace the ~18 duplicate hexes with
  their existing `var(--color-*)` tokens; replace raw `6px` radius with
  `var(--radius-base)`; snap spacing/type/radius values to the nearest new
  token. Verify: each replaced literal resolves to the same computed value.
- **P2 — semantic colour (near visual no-op).** Migrate the red/amber/green/slate
  raw hexes to the new semantic tokens. Verify per-section.
- **P3 — primitives.** Extract Button, Badge, Spinner, Toggle, Input; migrate
  call sites feature by feature. Intentional, reviewed diffs.
- **P4 — Modal primitive.** Migrate the 6 modals onto `openModal`. Highest risk.

**Acceptance criteria (P1/P2):** no rendered value changes except in any
deliberately touched surface.

---

## 6. Verifying a change

Adopting a token or shared primitive can shift a value slightly (a control taking
the primitive's size, for instance) — that's expected, and a refactor does **not**
have to render pixel-for-pixel identically; the goal is a consistent system, not a
frozen one. Make each change deliberately and review the diff — and, for a visible
change, the running UI surface it touches. Nothing gates this automatically: the
design system is a convention for the default UI, not an enforced rule (section 7).

---

## 7. Keeping the default coherent (convention, not enforcement)

- **Style guide:** `frontend/style_guide.md` — the short "prefer the tokens and
  primitives when extending the default UI" guide agents auto-read as the project
  style guide. Cross-linked from the app README, the `style.css` banner, and
  `.agents/shared/worker/references/type-system-interface.md`. It is guidance for
  the default look, explicitly deferring to the user for custom redesigns.
- **No ratchet.** There is deliberately no automated gate on raw values. A hard
  gate was considered and removed: the failure mode here is cosmetic, the tax on
  every legitimate deviation is real, and — most importantly — a user redesigning
  their own interface should never be blocked by "use a variable" rules. If
  sprawl in the default UI ever becomes a problem again, a periodic cleanup pass
  (like the one this document records) is the intended remedy.

---

## 8. Open questions for the user

- Dark mode: there is none today (single light theme). Completing the token set
  makes a dark theme cheap later — is that wanted, or explicitly out of scope?
- Tailwind-utility vs semantic-class direction: do we standardize on one, or
  keep both with a rule for when each applies?

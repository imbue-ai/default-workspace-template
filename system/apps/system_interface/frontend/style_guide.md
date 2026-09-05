# System Interface frontend style guide

Project-specific style guide for `system/apps/system_interface/frontend` (the web
workspace UI: TypeScript + Vite + Tailwind v4 + mithril/dockview). Read it
alongside the base `docs/system/style_guide.md` (which covers Python/backend
conventions and does not cover the frontend). This file governs the CSS and the
**design system** — the single most important convention for UI work here.

Styling lives in the markup as Tailwind utilities; `src/style.css` is a token
file plus a small set of escape hatches, not a stylesheet of component classes.

---

## Where styling goes

**Utilities in the markup are the default.** A view styles its own elements with
Tailwind utility classes in its `m(...)` class strings — layout, spacing, color,
type, borders, states (`hover:`, `focus-visible:`, `disabled:`) included. Do not
add a new `.feature-*` class to `style.css` for something a utility string can
say at the call site.

- **Colors** come from the semantic utility layer (`text-primary`,
  `text-secondary`, `text-faint`, `bg-page`, `bg-surface`, `bg-fill-hover`,
  `border-default`, `text-accent`, `bg-danger-surface`, …) — see the
  `@theme inline` block in `src/style.css`. Raw palette values live once, as
  `--c-*` on `:root`; hand-written CSS references those directly
  (`var(--c-accent)`), never a hex.
  - One name per token, with no aliases. Design work imported from elsewhere
    gets translated into these names on the way in, not registered under its own
    (`views/style-modules.test.ts` asserts every colour and `type-*` role the
    shared class-string modules use actually resolves — Tailwind v4 emits
    nothing for an unknown utility, so a stray name fails silently).
- **Type roles** are the `type-*` utilities (`type-heading-lg`, `type-heading`,
  `type-label`, `type-body`, `type-helper`, `type-section`) — use a role before
  hand-setting font-size/weight. For an off-role size, reference the token:
  `text-(length:--font-size-body)`; dense chrome (the rail, menus, launcher
  tables) sits on `--font-size-row` (13px).
- **Spacing** uses the stock scale (`p-N`/`gap-N`/`m-N`; 4px per step). Steps in
  use: 0.5/1/1.5/2/3/4/5/6/8. Off-grid pixel values are allowed where the design
  needs them (`h-[34px]`), as deliberate exceptions.
- **Radius/elevation**: `rounded-sm/md/lg/xl` (4/6/8/16) and `shadow-raised` /
  `shadow-overlay`.
**Shared, reusable primitives live in `src/views/components/`**; feature
screens and feature-specific helpers stay directly in `src/views/`. A primitive is a generic building block
with no feature knowledge (the Button and Modal components, the input/badge
recipes, the tooltip mechanism, the icon set); when a look starts being reused
across features, it belongs here.

- **Buttons are the `Button` component** in `src/views/components/Button.ts`:
  `m(Button, {variant, sm, xs, icon, round, selected, readonly, quiet, block,
extra, ...attrs}, children)` renders a real `<button type="button">`, applies
  the shared recipe, and passes through every attr it doesn't consume
  (`onclick`, `disabled`, `aria-*`, `oncreate`, ...). A caller's `class` never
  passes through — additive utilities/markers go in `extra`. The class recipe
  (`buttonClass()`) is exported from the same file for the rare place a
  component can't go: an element that must stay a real anchor (the login
  modal's OAuth link) or DOM built outside mithril (the lightbox). A dialog's
  confirming action is `variant: "primary"`; a lone dismiss/cancel keeps the
  `secondary` default; a destructive confirm is `"destructive"` (ghost form:
  `"ghost-destructive"`).
- **Other shared recipes** (a look used by more than one file) live one file
  per primitive under `components/`: `inputClass()` in
  `src/views/components/Input.ts`, `badgeClass()` in
  `src/views/components/Badge.ts`, and the `MODAL_*_CLASS` shell beside the
  Modal component in `src/views/components/Modal.ts` (shared type-size
  fragments: `src/views/components/typography.ts`). Feature-local sharing is an
  exported constant next to the owning view. Extend these rather than
  hand-rolling a lookalike. Input and badge stay class builders deliberately (a
  handful of call sites, no invariants a wrapper would enforce); promote one to
  a component the way Button was promoted if its usage grows.

**Keep the semantic class names as bare markers.** Every element keeps a
readable identity class (`queued-header`, `subagent-card--done`, `btn`,
`btn--primary`, `modal-card`, …) at the front of its class string. Markers carry
no styling; they are hooks for the vitest suites, the Python e2e tests, JS
queries, and the inspector. Never drop one without checking all three.

**The scanner must see every utility literally.** `style.css` uses
`source(none)` + `@source "./**/*.ts"`, and the scanner cannot evaluate code:
never build a utility name by string interpolation (markers are fine to
interpolate). If a utility genuinely cannot appear as a contiguous literal,
safelist it with `@source inline("...")`.

## What still belongs in `src/style.css`

- **Tokens**: the `--c-*` value table, the `@theme` blocks, and the `type-*`
  `@utility` roles.
- **Vendor DOM you don't render**: the dockview theme overrides (`.dv-*`),
  xterm, scrollbars.
- **Rendered content you don't render per-element**: markdown output
  (`.markdown-content …`), where classes can't be attached per element.
- **Pseudo-element/keyframe machines**: `@keyframes`, the `.spinner` — where
  the drawing lives in `::before/::after` or animation state.
- **Contextual rules over shared markup**: where markup renders in a context
  that cannot attach classes to it — e.g. the `.modal-card
  button[aria-pressed]` feedback over the settings pickers' inline-styled
  cells. Where the shared markup is an exported class constant instead (the
  user bubble recipes in `user-message-display.ts`), compose the constant
  rather than writing a contextual rule.
- **Cascade-critical overrides**: hand-written (unlayered) CSS beats utilities
  (which live in `@layer utilities`). An override of an unlayered rule must
  itself be CSS — a utility cannot win that fight. When migrating a rule to
  utilities, check what it overrides and what overrides it.

## Design system: an optional convention for the default UI

**It is a convention, not a rule, and it defers to the user.** If the user wants
their interface restyled to their own taste — including something bold, unique,
and nothing like the shipped default — build exactly that. Don't hold back,
don't "correct" it back toward tokens, and don't treat anything here as a gate.
Nothing enforces it; there is no ratchet.

### Review what you changed

Adopting a token or a shared primitive can shift a value slightly — a control
picking up the primitive's size, say. That's expected and fine; the goal is a
consistent system, not a pixel-identical one. Just make the change deliberately:
review the diff — and, for a visible change, the running UI — to confirm you
changed only what you intended.

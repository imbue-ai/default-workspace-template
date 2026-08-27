# System Interface frontend style guide

Project-specific style guide for `system/apps/system_interface/frontend` (the web
workspace UI: TypeScript + Vite + Tailwind v4 + mithril/dockview). Read it
alongside the base `docs/system/style_guide.md` (which covers Python/backend
conventions and does not cover the frontend). This file governs the CSS and the
**design system** — the single most important convention for UI work here.

The architecture deliberately mirrors the minds desktop client
(`mngr-internal/apps/minds/frontend`): styling lives in the markup as Tailwind
utilities; `src/style.css` is a token file plus a small set of escape hatches,
not a stylesheet of component classes. History and the full migration log live
in [`../docs/design-system.md`](../docs/design-system.md).

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
- **Type roles** are the `type-*` utilities (`type-heading-lg`, `type-heading`,
  `type-label`, `type-body`, `type-helper`, `type-section`) — use a role before
  hand-setting font-size/weight. For an off-role size, reference the token:
  `text-(length:--font-size-body)`.
- **Spacing** uses the stock scale (`p-N`/`gap-N`/`m-N`; 4px per step). Steps in
  use: 0.5/1/1.5/2/3/4/5/6/8. Off-grid pixel values are allowed where the design
  needs them (`h-[34px]`), as deliberate exceptions.
- **Radius/elevation**: `rounded-sm/md/lg/xl` (4/6/8/16) and `shadow-raised` /
  `shadow-overlay`.
- **Shared recipes** (a look used by more than one file) are class-string
  builders/constants in `src/views/primitives.ts` — `buttonClass()`,
  `inputClass()`, `badgeClass()`, the `MODAL_*_CLASS` shell — or an exported
  constant next to the owning view for feature-local sharing. Extend those
  rather than hand-rolling a lookalike. A dialog's confirming action is
  `buttonClass("primary")`; a lone dismiss/cancel stays quiet (`"secondary"`);
  a destructive confirm is `"destructive"` (quiet form: `"ghost-destructive"`).

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
- **Pseudo-element/keyframe machines**: `@keyframes`, the `.spinner`, the
  `.toggle`, the CSS tooltip — where the drawing lives in `::before/::after`
  or animation state.
- **Contextual rules over shared markup**: e.g. `.queued-message
  .message-user-bubble { opacity: … }` — the bubble markup comes from shared
  code, so context classes are the mechanism. The chat message tree
  (`.message`, `.message-user`, `.message-user-bubble`) stays CSS for this
  reason (and is queried from JS for row measurement/scroll selection).
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

Two items are about correctness, not taste, so keep them even in a custom
redesign:

- **Interactive elements are real `<button>` / `<a>`**, never clickable
  `<div>` / `<span>` — for accessibility and consistent keyboard/focus behaviour.
- **Every interactive element gets `:hover`, `:focus-visible`, and `:disabled`
  states.** The primitives carry these; reuse them.

### Review what you changed

Adopting a token or a shared primitive can shift a value slightly — a control
picking up the primitive's size, say. That's expected and fine; the goal is a
consistent system, not a pixel-identical one. Just make the change deliberately:
review the diff — and, for a visible change, the running UI — to confirm you
changed only what you intended.

---

## Running and testing

- Build: `npm run build` (must be clean). Lint: `npm run lint`. Test:
  `npm run test`. Format: `npm run format` (prettier sorts the utility strings
  via prettier-plugin-tailwindcss).
- The wider isolation / preview / reveal rules for changing the live UI are owned
  by the `update-system-interface` skill and
  `.agents/shared/worker/references/type-system-interface.md`. Never edit the
  served tree directly.

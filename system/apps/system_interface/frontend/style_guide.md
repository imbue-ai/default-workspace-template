# System Interface frontend style guide

Project-specific style guide for `system/apps/system_interface/frontend` (the web
workspace UI: TypeScript + Vite + Tailwind v4 + mithril/dockview). Read it
alongside the base `docs/system/style_guide.md` (which covers Python/backend
conventions and does not cover the frontend). This file governs the CSS and the
**design system** — the single most important convention for UI work here.

Full assessment, the target token set, and the migration plan live in
[`../docs/design-system.md`](../docs/design-system.md). This file is the short,
enforceable "what to do while editing" version.

---

## Design system: an optional convention for the default UI

The shipped **default** interface is built from a small design system — a token
layer (`@theme` at the top of `src/style.css`: colours, radius, spacing, type,
shadow, motion, z-index) plus shared component classes (`.btn`, the modal shell,
`.badge`, `.toggle`, `.spinner`, `.input`). It exists so the default look stays
coherent instead of regrowing the ~15 bespoke button families and ~45 raw colours
it started with.

**It is a convention, not a rule, and it defers to the user.** If the user wants
their interface restyled to their own taste — including something bold, unique,
and nothing like the shipped default — build exactly that. Don't hold back, don't
"correct" it back toward tokens, and don't treat anything below as a gate.
Nothing enforces it; there is no ratchet.

When you *are* extending or maintaining the default look, prefer the system so it
stays consistent:

- **Reach for a token before a literal.** `var(--color-*)` over a raw
  `#hex`/`rgb()`; `var(--radius-base)` over a raw `6px`; an existing type/spacing
  value over a brand-new one. If a colour has no token, adding a
  semantically-named one (e.g. `--color-danger`) keeps the default themeable.
- **Reuse a shared component before hand-rolling one.** Extend the `.btn`
  primitive (`.btn--{primary|secondary|ghost|destructive|inverse|icon}`, plus
  `.btn--sm`, `.btn--selected`, and the `.btn--ghost.btn--destructive`
  quiet-destructive combo) or the modal/badge/toggle/spinner/input primitive,
  rather than copying it under a new feature prefix. A dialog's default action
  (its confirm, or the sole button in a one-button notice) is `.btn--primary`;
  downgrade only when the content calls for it.
- **Prefer semantic tokens/classes over Tailwind utilities for anything themed**
  (colour, and spacing that should track the scale). Utilities are fine for
  genuinely one-off layout (`flex`, `grid`, `gap`).

Two items below are about correctness, not taste, so keep them even in a custom
redesign:

- **Interactive elements are real `<button>` / `<a>`**, never clickable
  `<div>` / `<span>` — for accessibility and consistent keyboard/focus behaviour.
- **Every interactive element gets `:hover`, `:focus-visible`, and `:disabled`
  states.** Reuse the existing selected/active spelling rather than inventing a
  fifth.

### Review what you changed

Adopting a token or a shared primitive can shift a value slightly — a control
picking up the primitive's size, say. That's expected and fine; the goal is a
consistent system, not a pixel-identical one, so don't contort the code to keep
an exact visual match. Just make the change deliberately: review the diff — and,
for a visible change, the running UI — to confirm you changed only what you
intended.

---

## Running and testing

- Build: `npm run build` (must be clean). Lint: `npm run lint`. Test:
  `npm run test`.
- The wider isolation / preview / reveal rules for changing the live UI are owned
  by the `update-system-interface` skill and
  `.agents/shared/worker/references/type-system-interface.md`. Never edit the
  served tree directly.

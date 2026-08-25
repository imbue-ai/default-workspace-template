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

## Design system: use it; don't drift

**Why this matters.** Left unmanaged, this UI grew ~15 bespoke button families,
6+ independent modal implementations, 29 font sizes, 23 spacing values, and ~45
raw hex colours (a third of them exact duplicates of existing tokens). Every new
one-off makes the next change harder and the UI less coherent. The token layer
(`@theme` at the top of `src/style.css`) and the shared component classes exist
so you don't have to reinvent — **reach for them first, every time.**

### The rules

1. **Colour — never write a raw `#hex` / `rgb()` / `rgba()`** in `style.css` or in
   inline styles. Use a `var(--color-*)` token from the `@theme` block. If the
   colour you need has no token, **add a semantically-named token** (e.g.
   `--color-danger`), don't inline a literal. The same value must never exist as
   both a token and a hardcoded copy.

2. **Type, spacing, radius, shadow, motion, z-index — use the token**, or the
   nearest existing value; do not invent a new literal. The radius token
   `--radius-base` already exists — use it instead of retyping `6px`. (Type and
   spacing scales are mid-rollout; see `design-system.md` P1. Until a scale token
   exists, **reuse an existing value rather than adding a new one** — do not widen
   the set.)

3. **Components — reuse the shared class/primitive; do not hand-roll another**
   button, modal, badge, toggle, spinner, or input. Extend the existing one (or
   add a variant modifier) instead of copying it under a new feature prefix. For
   buttons, use the `.btn` primitive: `.btn.btn--{primary|secondary|ghost|
   destructive|inverse|icon}`, with `.btn--sm`, `.btn--selected`, and the
   `.btn--ghost.btn--destructive` quiet-destructive combo. Grow shared primitives
   in that shape rather than making per-feature copies. (Modal, badge, toggle,
   spinner, and input primitives are still to come — until they land, generalize
   the best existing example rather than adding another one-off.)

4. **Prefer semantic tokens/classes over Tailwind utilities for anything
   themed** (colour, and spacing that should track the scale). Tailwind utilities
   are fine for genuinely one-off, non-tokenized layout (`flex`, `grid`, `gap`).
   Don't express a themed colour or a design-token spacing as a utility.

5. **Interactive elements are real `<button>` / `<a>`**, never clickable
   `<div>` / `<span>` — for accessibility and so they inherit consistent states.

6. **States** — every interactive element gets `:hover`, `:focus-visible`, and
   `:disabled`. Use the existing selected/active convention; do not invent yet
   another spelling (the codebase already has four: `--selected`, `--active`,
   `-current`, `.dv-active-tab` — do not add a fifth).

### When you may deviate (the escape hatch)

Deviation is allowed **only when a token or primitive genuinely cannot express
what's needed** — not to save a few minutes. It is meant to be rare and visible.
When you must:

- Keep it minimal and local to the one site that needs it.
- Add a `/* design-system-exception: <reason> */` comment at that site.
- In the **same change**, bump the matching baseline in
  [`src/design-system-ratchet.test.ts`](src/design-system-ratchet.test.ts) with a
  comment saying why. That ratchet only ever ratchets *down*, so a bump is an
  explicit, reviewable decision — never a silent regression. Do **not** evade the
  ratchet by reformatting to dodge its regex; that is worse than the raw value.

### Prove you didn't regress

A token-only refactor should be a visual no-op: each replaced literal must resolve
to the same computed value. The ratchet
([`src/design-system-ratchet.test.ts`](src/design-system-ratchet.test.ts)) runs
under `npm run test` and fails when raw hex / font-size / border-radius / z-index
values grow, so a regression that reintroduces a literal is caught automatically.
Review the diff — and, for a genuine visual change, the running UI — to confirm you
changed only what you intended.

---

## Running and testing

- Build: `npm run build` (must be clean). Lint: `npm run lint`. Test:
  `npm run test` (includes the design-system ratchet and the lint/format check).
- The wider isolation / preview / reveal rules for changing the live UI are owned
  by the `update-system-interface` skill and
  `.agents/shared/worker/references/type-system-interface.md`. Never edit the
  served tree directly.

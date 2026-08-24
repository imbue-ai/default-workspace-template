Added local development guidelines that push future agents to use the design system, backed by an enforcement ratchet. No user-facing behaviour change.

- `frontend/style_guide.md`: the enforceable "use tokens and shared component primitives; here's the narrow escape hatch" rules for UI work (agents auto-read a project's `style_guide.md`). Colours must be `var(--color-*)` tokens, radius `var(--radius-base)` etc.; reuse shared button/modal/badge classes instead of hand-rolling new ones; interactive elements are real `<button>`/`<a>`.

- `frontend/src/design-system-ratchet.test.ts`: a vitest ratchet that scans `src/style.css` and fails when raw hex colours (outside `@theme`), raw `px` in `font-size`/`border-radius`, or raw numeric `z-index` exceed recorded baselines. Baselines only ratchet down; a justified deviation raises the baseline with a comment (the escape hatch). Baselines recorded at the current pre-sweep state.

- Discoverability: a design-system banner at the top of `src/style.css`, a "Design system" section in `.agents/shared/worker/references/type-system-interface.md` (the reference agents read when editing this UI), and a pointer in the app README. Cross-linked with `docs/design-system.md`.

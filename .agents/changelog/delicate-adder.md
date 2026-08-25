Added a "Design system: use it, don't drift" section to the worker reference
`shared/worker/references/type-system-interface.md` (the doc agents read when
editing the system-interface UI). It tells future agents to style with the new
`var(--color-*)`/`var(--radius-*)` tokens and to reuse the shared
button/modal/badge/toggle/spinner primitives rather than hand-rolling per-feature
copies, keeps interactive elements as real `<button>`/`<a>`, and documents the
narrow escape hatch: mark a justified raw value with a `design-system-exception`
comment and bump the matching baseline in `design-system-ratchet.test.ts` in the
same change. Points at the app's `frontend/style_guide.md` and
`docs/design-system.md` for the full rules and token set.

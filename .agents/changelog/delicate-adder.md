Added a "Design system" section to the worker reference
`shared/worker/references/type-system-interface.md` (the doc agents read when
editing the system-interface UI). It points agents at the frontend's token layer
(`var(--color-*)`/`var(--radius-*)`) and shared button/modal/badge/toggle/spinner
primitives, and asks them to reuse those rather than hand-rolling per-feature
copies when extending the default look. It is framed as an optional convention,
not a rule: if the user wants their interface restyled to their own taste, the
agent should build that and not steer it back toward tokens (there is no ratchet
enforcing any of it). The one hard rule kept is accessibility — interactive
elements stay real `<button>`/`<a>`. Points at the app's `frontend/style_guide.md`
and `docs/design-system.md` for the token set and background.

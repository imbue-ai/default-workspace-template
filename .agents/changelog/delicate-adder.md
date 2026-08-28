Added a "Design system" section to the worker reference
`shared/worker/references/type-system-interface.md` (the doc agents read when
editing the system-interface UI). It describes the frontend's convention --
styling as Tailwind utilities in the markup over a semantic token layer, with
shared primitives (the Button and Modal components, the input/badge recipes) to
reuse rather than hand-rolling per-feature copies when extending the default
look. It is framed as an optional convention, not a rule: if the user wants
their interface restyled to their own taste, the agent should build that and
not steer it back toward tokens (there is no ratchet enforcing any of it). The
one hard rule kept is accessibility -- interactive elements stay real
`<button>`/`<a>`. Points at the app's `frontend/style_guide.md` for the full
guide.

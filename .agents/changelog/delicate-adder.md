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

Aligned the Claude sign-in modal's backdrop with the shared modal conventions:
it now closes on Escape (through the shell's shared Escape handler), and its
overlay sits on the --z-overlay layer with the standard flat dim. Previously it
floated at a historical z-50 with a 3px backdrop blur, so once the rail moved
onto --z-sticky the sidebar painted above the dim and stayed clickable while
the dialog was up.

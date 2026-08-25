Added a design-system assessment for the frontend. No user-facing behaviour change.

- `docs/design-system.md`: a durable assessment of how the UI is built and a phased plan to systematize it. Measures the current sprawl (29 font sizes, 23 spacing atoms, 18 radii, 7 unmanaged z-indexes, ~15 bespoke button families, 6+ independent modal implementations, ~45 raw hex colours of which ~18 duplicate existing tokens), proposes a complete `@theme` token set and a handful of component primitives, and lays out a low-risk migration (P0 scaffold → P1 mechanical sweep → P2 semantic colour → P3/P4 primitives + modal).

(A dev-only visual-diff gallery under `frontend/gallery/` was added alongside this to prove each migration phase a no-op; it was removed once the migration completed.)

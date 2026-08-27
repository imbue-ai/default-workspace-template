Design-system pass over the system interface frontend: a token layer, shared
component primitives, and modal consolidation. A styling and structure change
with no behavioural change. Most values are preserved; a few controls shift size
slightly where they adopt a shared primitive (the progress spinner ring goes
1.5px -> 2px, launcher section headings 11px -> 12px, icon buttons onto the
shared 34px `.btn--icon` size), which is intended -- the goal is a consistent
system, not a pixel-identical render.

- **Token scaffold in `src/style.css`.** Colours are now semantic
  `var(--color-*)` tokens from a single `@theme` block (zero raw hex outside
  `@theme`); every colour is role-named (`bg`, `surface`, `text-*`, `accent*`,
  `danger`/`warning`/`success`, `inverse-surface`, ...) with no scale-position
  tokens -- the transitional `--color-neutral-*` slate ramp was removed and its
  one live use (the terminal banner) moved to `--color-surface-secondary`. The
  `@theme` block is kept fully live: seven defined-but-unused tokens
  (`--color-info`, `--ease-standard`, `--elevation-md`, `--spacing-page`,
  `--width-sidebar`/`-collapsed`, `--duration-sidebar-transition`) were pruned so
  every token is referenced by the shipped CSS. Corner radii collapse onto a
  `--radius-*` scale, spacing onto Tailwind's 4px spacing scale, motion
  timing/easing, drop shadows onto an elevation scale, and the former magic
  z-index numbers onto named tokens. Spacing is a **single knob** — Tailwind's
  stock 0.25rem `--spacing` base: the `p-N`/`m-N`/`gap-N`/`w-N` utilities generate
  from it, and hand-written CSS references the same scale via the `--spacing(N)`
  function (`padding: --spacing(3)` compiles to `calc(var(--spacing) * 3)`), so
  markup and CSS share one source with no per-step token to define or keep in
  sync.

- **Shared component primitives.** A unified `.btn` (with `round`/`filled`/`stop`
  and `ghost-destructive` variants), plus `.badge`, `.input`, `.toggle`, and a
  `.spinner` that collapses the three duplicate spin keyframes into one. The
  bespoke per-feature buttons, badges, spinners, and text-tooltip bubbles across
  the composer, close-×, image lightbox, queued-action, terminal banner,
  permission request, and the modals were migrated onto these.

- **Shared modal shell.** A new `Modal` primitive (`src/views/Modal.ts`); the
  destroy-confirm dialog, fast-mode prompt, share modal, add-to-project dialog,
  project settings modal, composer command notices, and terminal sign-in notice
  all now render through it. Removed the dead custom-url-dialog CSS.

- **Dialog emphasis.** Buttons: a dialog's confirming action -- a real choice it
  asks the user to make (Save, Continue, the default in a two-option prompt) --
  is `.btn--primary`, while a button that only dismisses or acknowledges (a lone
  OK/Close, a Cancel, a Back) stays quiet (`.btn--secondary`) and a destructive
  confirm is `.btn--destructive`; the rule is recorded in
  `frontend/style_guide.md`. Body copy: `.modal-message` / `.modal-body` now read
  at `--color-text-primary` instead of the muted `--color-text-secondary`, so
  every dialog's body text reads at full strength; a genuinely secondary line
  opts into the muted colour at its own call site.

- **Removed the dev-only visual-diff gallery harness** (no longer needed after
  the sweep).

- **Guidance (not enforced).** `frontend/style_guide.md` and
  `docs/design-system.md` document the tokens and shared primitives as an
  optional convention for keeping the *default* UI coherent — explicitly not a
  hard rule, so a user redesigning their interface to their own taste isn't
  constrained by it. There is deliberately no automated ratchet gating raw
  values (an earlier draft added one; it was removed to avoid impeding
  user-driven redesigns). Discoverability: a banner at the top of
  `src/style.css`, the token set and background in `docs/design-system.md`, and a
  pointer in the app README.

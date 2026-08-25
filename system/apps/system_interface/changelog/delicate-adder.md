Design-system pass over the system interface frontend: a token layer, shared
component primitives, and modal consolidation. A styling and structure change
with no behavioural change. Most values are preserved; a few controls shift size
slightly where they adopt a shared primitive (the progress spinner ring goes
1.5px -> 2px, launcher section headings 11px -> 12px, icon buttons onto the
shared 34px `.btn--icon` size), which is intended -- the goal is a consistent
system, not a pixel-identical render.

- **Token scaffold in `src/style.css`.** Colours are now semantic
  `var(--color-*)` tokens from a single `@theme` block (zero raw hex outside
  `@theme`); corner radii collapse onto a `--radius-*` scale, spacing onto a 4px
  scale, motion timing/easing, drop shadows onto an elevation scale, and the
  former magic z-index numbers onto named tokens.

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

- **Dialog button emphasis.** A dialog's default action -- its confirm, or the
  sole acknowledgement in a one-button notice -- now renders `.btn--primary`, and
  a button is downgraded to secondary/ghost/destructive only when its content
  calls for it. Promoted the share notice's "Close", the terminal sign-in
  notice's "OK", and the declined-command notice's "OK" from secondary to
  primary; the rule is recorded in `frontend/style_guide.md`.

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

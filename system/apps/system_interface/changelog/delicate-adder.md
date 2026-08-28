Design-system pass over the system interface frontend: a semantic token layer,
styling moved into the markup as Tailwind utilities (architecture parity with
apps/minds), shared component primitives, and modal consolidation. A styling and
structure change with no behavioural change. Most values are preserved; a few
controls shift size slightly where they adopt a shared primitive, and there are
a few deliberate recolours (one unified warm danger red, neutral progress
greys, body text 15px -> 14px) -- the goal is a consistent system, not a
pixel-identical render.

- **Utilities in the markup; `style.css` reduced to tokens + escape hatches.**
  Each view now styles its own elements with Tailwind utility classes in its
  `m(...)` class strings; the ~29 feature-namespaced CSS silos are dissolved.
  What remains in `src/style.css` (4,125 -> ~1,290 lines) is there by design:
  the token layer, vendor DOM overrides (dockview/xterm, scrollbars), markdown-
  rendered content, keyframes and pseudo-element machines (the one `.spinner`),
  contextual rules over shared markup, and cascade-critical overrides. Semantic
  class names (`btn--primary`, `modal-card`, `message-user`, ...) stay in the
  markup as bare, style-free markers for the test suites and JS queries.

- **Token layer.** Colours live once as a `--c-*` value table on `:root`, with
  an `@theme inline` layer generating the semantic utilities the views use
  (`text-primary`, `bg-fill-hover`, `border-default`, `bg-danger-surface`, ...);
  zero raw hex outside the token table. Type is six role utilities
  (`type-heading-lg` ... `type-section`) over `--font-size-*`/`--weight-*`
  tokens. Radius and elevation align with the minds design system
  (`rounded-sm/md/lg/xl` = 4/6/8/16, `shadow-raised`/`shadow-overlay`); motion
  is `--dur-*`; z-index magic numbers became named `--z-*` layers. Spacing is a
  single knob -- Tailwind's stock 0.25rem `--spacing` base -- shared by the
  `p-N`/`gap-N` utilities and hand-written CSS via the `--spacing(N)` function.

- **Shared primitives as view modules, under `src/views/components/`.** The
  generic, reusable building blocks live in their own `views/components/`
  subfolder (mirroring the minds client), with feature screens and
  feature-specific helpers staying directly in `views/`. A `Button` component
  (`components/Button.ts`: variants primary/secondary/ghost/destructive/
  ghost-destructive/inverse/ghost-inverse/stop; sm/xs/icon/round/block options;
  `selected` -- the on/off toggle recipe that replaced the old `.toggle` -- and
  `readonly` for non-interactive-but-explained controls), plus `inputClass`
  (`components/Input.ts`), `badgeClass` (`components/Badge.ts`), a single CSS
  `.spinner`, one shared collapsible tool block (`components/ToolCallBlock.ts`),
  the shared `Modal` shell and `NoticeDialog`, and one tooltip mechanism (the
  JS hover tooltip; the CSS `data-tooltip` bubble is gone). The bespoke
  per-feature buttons, badges, spinners, toggles, and tooltip bubbles across
  the composer, lightbox, queued actions, terminal banner, permission card,
  sidebar micro-controls, and the modals were migrated onto these.

- **Shared modal shell.** A `Modal` component (`components/Modal.ts`) owning the
  overlay/card/header/actions chrome and backdrop dismissal
  (`components/modalBackdrop.ts`: primary mousedown on the overlay itself); the
  destroy-confirm dialog, fast-mode prompt, add-to-project dialog, project
  settings modal, terminal sign-in notice, and the shared notice dialog
  (`components/NoticeDialog.ts` -- the declined-command, auth, and send-failure
  notices) all render through it.
  Deliberately left off the shell: the full-bleed image lightbox and the
  multi-step Claude sign-in modal (forcing either on would regress it).

- **Dialog emphasis.** A dialog's confirming action -- a real choice it asks the
  user to make -- is the primary Button; a button that only dismisses or
  acknowledges stays quiet (secondary), and a destructive confirm is
  destructive (quiet form: ghost-destructive). Body copy reads at the primary
  text colour; a genuinely secondary line opts into the muted colour at its own
  call site.

- **Guidance (not enforced).** `frontend/style_guide.md` documents the
  convention (where styling goes, what stays CSS, the primitives, the Tailwind
  scanner's contiguous-literal rule). It is an optional convention for the
  default UI -- a user redesigning their interface to their own taste isn't
  constrained by it, and there is deliberately no ratchet gating raw values (an
  earlier draft added one; it was removed to avoid impeding user-driven
  redesigns).

- **Test infrastructure.** Removed the dev-only visual-diff gallery harness
  used during the migration.

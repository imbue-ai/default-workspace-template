# Brand design system as the Standard theme

> In mind-sketches/prototypes/component-audit/ and mind-sketches/prototypes/color-themes/ we've fleshed out our branded design system that should be the default system/theme in def-work-template. Let's build out a plan of conversion from the status quo to this and how it relates to: (1) bring all apps to use as many default components as possible, (2) how it affects theming, (3) how to remap css variables, (4) anything else
> * The brand replaces `standard` in place (same id, new values), so every workspace changes look on update.
> * When the prototypes disagree, component-audit/studio-brand decides component looks and sizes and color-themes decides the color system. Any other inconsistencies found while building get raised with the user.
> * Full rename to the role vocabulary as theme contract 2, done across every app, theme and test in one pass, with no alias layer.
> * Fonts: Atkinson Hyperlegible Next/Mono for UI and Besley for display, all open-licensed and bundled.
> * Every app that ships with the template uses shared components: shell, chat, files, getting started, routines, style guide and system monitor, adding shared components wherever one is missing.
> * Screen and the terminal page keep their current look and controls for now (out of scope).
> * Every prototype component an app needs is ported into the shared Mithril library (forms, status, surfaces, overlays, popovers, tables and so on).
> * The shared components keep the current workspace_ui icon set for now.
> * The Style Guide app is rebuilt after the prototype: ramps, roles, the type ramp, every new component, and a workspace-color preview picker.
> * Enforcement (lint rules, a zero-violation test gate) is left to a separate PR.
> * Work is split into PRs. PR 1 is the foundation: contract 2, tokens, fonts, the workspace-color pipeline, the theme ports and migration, and a mechanical rename in every app. After that, one PR per batch of new components plus the apps that adopt them.
> * The prototype's "theme" means a color change inside one theme, not a template theme. There is exactly one Standard theme, and its dynamic colors all derive from the workspace color (the existing Imbue Studio setting). Other themes can do whatever they want.
> * The shell reads the workspace's `color` label through mngr (on boot and on change) and pushes it to every app with `shell:theme`. Plain HTML pages get it through the page kit.
> * Imbue Studio also posts the new color into the shell frame after writing the label, so a change shows instantly with no polling. This is a paired change in mngr-internal, which is in scope.
> * With no color label, the workspace uses Courage (#492222), the brand's precomputed default.
> * The workspace color drives a full derivation as in `seedTheme`: primary is the color exactly; neutral, accent, status (harmonized) and wallpaper follow from it.
> * The default wallpaper is a solid wallpaper ramp step derived from the workspace color, with the "studio" wordmark mark.
> * Keys are computed once in Python on the shell side (porting `solveL`/`harmonize`/APCA for contrast and gamut safety) and sent as variables. Ramps and hover/active stay pure CSS.
> * Keys reach pages two ways: each app serves a generated `workspace_color.css` (with ETag) for first paint and for plain pages, and `shell:theme` carries them for live changes.
> * The workspace keys and ramps are always present as contract tokens. Any theme may use or override them; Classic Mac and Windows 2000 override.
> * Classic Mac and Windows 2000 are ported to contract 2 in the same change, and a one-time migration rewrites user-made themes' token names automatically.
> * Chat-shaped tokens (`bg-chat`, `bg-user-bubble`, `stop-button`, ...) leave the contract and map to roles in chat's own CSS. Every feature-specific variable in every app (`--files-*`, `--ink-*`, the start-glyph hues, `--desk-*` colors) is remapped to point at color-system variables as each app is worked through.
> * Desktop chrome takes the prototype's metrics and states (focus-shaded title bar, no window border, round launcher field, flat shortcuts). The tray changes (presence strip, desktops widget) are left out.
> * System Monitor is rebuilt as a Mithril/Vite frontend like the other apps, using the shared components directly.
> * Visual check: before/after Playwright screenshots of every app and the theme gallery for human review, plus DOM measurements of key component sizes against the studio-brand spec.

## Overview

- **One Standard, rebranded in place.** `system/themes/standard` keeps its id; its values (compiled into `workspace_ui/src/base.css` and the shell's `theme/default.css`) become the Imbue brand. No new theme id, no opt-in.
- **Theme contract 2 is a role vocabulary.** It replaces the roughly 40 flat, partly app-shaped `--c-*` names with: one key per ramp, 12-step ramps computed from keys in CSS, and semantic roles (body, overlay, inverse, primary/accent/success/warn/danger surface and content, borders, wallpaper). Hover and active are computed, never tokens. The rename is total, with no aliases.
- **The workspace color is the only color input for Standard.** Imbue Studio's per-workspace `color` label is read by the shell, which turns it into keys in Python (contrast- and gamut-safe, ported from the prototype's `oklch.ts` and `seedTheme.ts`). Each app serves the keys as `workspace_color.css`. Ramps, roles and states follow in pure CSS.
- **Themes keep their freedom.** Keys and ramps are always present, so any theme may build on them or ignore them. Classic Mac and Windows 2000 are ported mechanically and keep their fixed palettes. User-made themes are migrated once, automatically.
- **Shared components everywhere they fit.** The Mithril `workspace_ui` library is restyled to the studio-brand sizes and grows every prototype component the shipped apps hand-roll today (switch, checkbox, notice, status pill, card, overlay/sheet, lightbox, popover/listbox, table). Each app then drops its hand-rolled versions. Screen and the terminal page are left alone.
- **Sources of truth.** Component looks and sizes come from `prototypes/studio-brand` (rendered by component-audit "after"). The color system comes from `prototypes/color-themes/src/styles/workspace.css` and `src/theme/*`. Any conflict not covered by that split goes back to the user, not into a guess.
- **Delivered as a foundation PR plus component-batch PRs.** The foundation leaves every app working, recolored and renamed. Each later PR adds a batch of components and converts the apps that use them.

## Expected behavior

**Look**
- After updating, every workspace on Standard wears the Imbue brand: warm paper neutrals, near-black ink, dark primary buttons, tinted status pills, Atkinson Hyperlegible UI type with Besley headings.
- Buttons are 32px with a 12px radius. Fields are 36px with a 2px focus ring, and errors show as border plus message, never a fill. Menus use a 16px card with 32px rows inset 4px. Cards have a 16px radius. Elevation has two levels (raised, floating), cast in neutral 12 instead of black.
- Window title bars shade by focus: the secondary surface when focused, 5% darker when behind. Windows have no border; the shadow separates them. The launcher field is round, shortcuts sit flat, and the tray is unchanged.
- The default wallpaper is a solid wallpaper-ramp step with the "studio" wordmark rising off the bottom edge. Wallpapers chosen before the update stay chosen.

**Workspace color**
- A workspace whose Imbue Studio color is Courage, or which has no color label (local docker, tests, older workspaces), shows the hand-tuned Imbue keys: Courage primary, green accent, warm neutral, Honey-derived wallpaper.
- Any other color becomes the primary exactly. Neutral, accent, the status hues and the wallpaper are derived from it. White text on every derived key meets 4.5:1. The text on the primary itself is black or white by APCA polarity, lightly tinted.
- Changing the color in Imbue Studio recolors every open app at once, without a reload.
- A workspace opened in a plain browser (a sharing link) gets no message from Imbue Studio. It wears the label as the shell last read it, and the shell rereads the label on every boot.
- An older Imbue Studio paired with a newer workspace, or the reverse, degrades quietly: the color still applies on the shell's next boot.
- Pages paint in the right colors on first frame. There is no flash of Courage before the workspace color arrives.

**Themes**
- Classic Mac and Windows 2000 look as they do today. They override the roles, so the workspace color does not reach them unless a theme opts in by referencing keys or ramps.
- User-made themes in the workspace's `themes/` folder keep working. On first boot after the update, their token names are rewritten to contract 2 and their `theme.toml` moves to `contract = 2`. Tokens with no contract-2 target (the `-hover` ones) are dropped, with a comment left in the file.
- A contract-1 theme that appears later (copied in by hand, or from an old template) is listed as unavailable with a problem saying to migrate it. `workspace-themes migrate <id>` fixes it, and the create-theme skill knows to run it.
- The theme validator rejects `--c-*-hover` and `--c-*-active` declarations, and contract-1 names, in contract-2 themes.

**Apps**
- Shell, chat, files, getting started, routines, style guide and system monitor use shared components for every button, field, switch, checkbox, menu, dropdown, dialog, notice, toast, tooltip, badge, status pill, popover, overlay and table they show.
- System Monitor follows themes and the workspace color for the first time.
- The Style Guide shows the ramps, the roles grouped as in the prototype, the studio-brand type ramp, and every component in every variant and state, with a workspace-color preview picker that recolors the page locally without changing the workspace.
- Screen and the terminal page look exactly as they do today.
- New apps from build-app inherit the brand automatically, through `base.css` for Vite apps and the page kit for plain pages.

## Implementation plan

### Contract 2 (`system/libs/workspace_themes`)

- `src/workspace_themes/contract.py`
  - `CONTRACT_VERSION = 2`. Replace the contract-1 `COLOR_TOKENS` with contract-2 groups:
    - `KEY_TOKENS`: `--c-{neutral,primary,accent,success,warn,danger,wallpaper}-key`
    - `RAMP_TOKENS`: `--c-{role}-{1..12}` for the same seven
    - `ROLE_TOKENS`:
      - body: `--c-body-surface`, `-surface-secondary`, `-surface-raised`, `--c-body-content`, `-content-secondary`, `-content-tertiary`
      - overlay: `--c-overlay-surface`, `--c-overlay-scrim`
      - inverse: `--c-inverse-surface`, `--c-inverse-content`, `-content-secondary`, `-content-tertiary`
      - per role (primary, accent, success, warn, danger): `-surface`, `-content`, `-subtle-surface`, `-subtle-content`
      - `--c-fill-faint`
      - borders: `--c-border-default`, `--c-border-subtle`, `--c-border-focus`
      - wallpaper: `--c-wallpaper-surface`, `--c-wallpaper-mark`
      - elevation: `--c-shadow-raised`, `--c-shadow-overlay`
    - `STATE_TOKENS`: `--state-ratio`, `--state-flare`, `--state-inactive` (themeable dials for computed hover/active)
  - Keep the type, shape, motion, `--desk-*` and `--term-*` groups. Type changes follow the studio-brand ramp (see `base.css` below).
  - Add a `CONTRACT_1_RENAMES: Mapping[str, str | None]` table (old name to new name, `None` = dropped). It drives both the migration and the validator's "this is a contract-1 name" message.
- `src/workspace_themes/validation.py`
  - Reject `--c-*-hover` and `--c-*-active` declarations, and any `CONTRACT_1_RENAMES` key.
  - Accept `contract = 2` only. A `contract = 1` theme becomes a `ThemeEntry` problem pointing at `migrate`.
- New `src/workspace_themes/migration.py`, pure: `migrate_style_text(text) -> MigrationResult` (rewritten text plus the dropped names) and `migrate_theme_folder(path)`. It rewrites `var()` references and declarations in `tokens.css`, `parts.css` and `apps/*.css`, and bumps `theme.toml`.
- New `src/workspace_themes/workspace_color.py`, pure, ported from `color-themes/src/theme/oklch.ts` and `seedTheme.ts`:
  - `measure`, `fit` (gamut map by chroma), `contrast` (WCAG), `apca`, `solve_l`, `harmonize`, `turn`
  - `IMBUE_PRIMARY = "#492222"`, `is_imbue(hex)`
  - `derive_workspace_keys(hex) -> WorkspaceKeys`, a frozen model holding the seven keys plus `primary_content`. It returns `None` for Imbue/Courage, so `base.css`'s hand-tuned keys stand.
  - `render_workspace_color_css(keys) -> str`, a `:root { ... }` block, empty for Imbue.
- `src/workspace_themes/flask_routes.py`: `register_workspace_theme_route` also serves `GET /_static/workspace_color.css`. It reads the shared state file (below), renders it with an ETag of the hex, and sends it with no-cache like `theme.css`.
- `src/workspace_themes/cli.py`: add `migrate [<id>|--all]` and `workspace-color <hex>` (prints the derived keys and their contrast, for debugging and for the create-theme skill).
- `pyproject.toml` import-linter layers: place `workspace_color` and `migration` beside `validation`, below `catalog`.

### Standard values (`system/libs/workspace_ui/src/base.css`, shell `theme/default.css`)

- `base.css` `:root`:
  - The color-themes `workspace.css` table: Imbue keys, the shared lightness ladder, neutral and color chroma curves, roles, fills, borders, wallpaper, elevation.
  - Scoped to `:root` instead of `.desk-root`.
  - The "Imbue Studio bridge" group is not carried over.
- `@theme inline`: drop every contract-1 alias (`--color-primary`, `--color-page`, `--color-accent`, `--color-danger-surface`, ...), so the old utility names fail to generate instead of silently meaning something new. Add the role utilities and the computed `-hover`/`-active`/`-inactive` utilities from `workspace.css`. `bg-fill-hover` and `bg-fill-active` become `neutral-3` and `neutral-4`.
- Type:
  - Replace the `type-*` utilities with the studio-brand ramp: heading-lg, heading, title-page, title, body, body-dense, label-large, label, helper, section, subsection, code, code-small, badge.
  - Map `--font-sans` to Atkinson Hyperlegible Next, `--font-mono` to Atkinson Hyperlegible Mono and `--font-display` to Besley.
  - Fold or remove `--font-size-row` and `--font-size-tag` per the studio-brand ramp. If that ramp conflicts with color-themes' "no row step" rule, raise it with the user.
- Fonts: new `system/libs/workspace_ui/src/fonts/` holding the Atkinson Next and Mono masters (400/500/600/700) and Besley, plus their OFL texts. Declared by `@font-face` in a new `src/fonts.css` imported by `base.css`, and served same-origin by each app's Vite build. Add the licenses to `THIRD_PARTY_NOTICES.md` and `licenses/`.
- `page/workspace_theme.css` (page kit): regenerate from the new `base.css` and add the `workspace_color.css` link to `page/workspace_theme.ts`.
- `system/apps/system_interface/frontend/src/theme/default.css`:
  - Remap the color-bearing `--desk-*` values to roles: `--desk-taskbar-surface` becomes body-surface, `--desk-backdrop` becomes wallpaper-surface, and the avatar colors become a primary or danger ramp step.
  - `--desk-default-wallpaper` becomes the new bundled "studio" wallpaper.
  - Metrics already match the prototype (36/48/12); keep them.

### Workspace color pipeline (shell)

- New `system/apps/system_interface/imbue/system_interface/shell/workspace_color.py`:
  - `read_workspace_color_label()` finds the workspace's primary agent (`labels.workspace == $MINDS_WORKSPACE_NAME`, the agent carrying `color`) through `mngr list --format json` and returns the normalized hex or `None`.
  - `WorkspaceColorStore` writes `data/.state/workspace_color.json` (`{"hex": ..., "revision": ...}`), the one file every app's route reads.
- Triggers:
  - Shell boot.
  - The new embedder message `minds:workspace-color` (below), relayed through `embedder_messages.py`. It triggers a reread of the label; the shell trusts the label, not the message body.
- `theme_wire.py` and `shell:theme`: add `workspaceColor: {hex, revision}`. The shell sends `themes_changed` when the revision changes.
- `workspace_ui/src/themes/themeClient.ts`:
  - `wearWorkspaceColor(revision)` keeps one `<link data-workspace-color href="/_static/workspace_color.css?v=...">` immediately before the theme link, so a theme always wins over keys.
  - `wearTheme()` calls it from `shell:theme`.
- `themes/themeBoot.ts`: the inline boot script inserts the remembered `workspace_color.css` link before first paint, as it does for the theme.
- Cascade, lowest to highest: `base.css` (Imbue keys, ramps, roles), then `workspace_color.css` (keys only), then `theme.css` (theme overrides).
- Every app that calls `register_workspace_theme_route` (chat, terminal, files, getting started, style guide, and system monitor once rebuilt) gets the route for free. The shell's `register_theme_static_route` gets the same.

### Imbue Studio side (mngr-internal, paired PR)

- `apps/minds/docs/embed-contract.md`: add the embedder-to-workspace type `minds:workspace-color` with payload `{ hex }`, and bump the version history. This follows the tolerant policy: it is a new type, so older receivers ignore it.
- The Studio frontend's frame messaging (next to where it sends `minds:permission-resolutions`):
  - Send it to every frame mounted on that workspace right after a successful color write. The write is `set_workspace_color` in `apps/minds/imbue/minds/desktop_client/workspace_settings.py`, reached from the workspace options page and `SettingsGroups.ts`.
  - Also send it after `minds:workspace-ready`, as a snapshot, so a frame that loads mid-change catches up.
- Tests beside the existing ones (`workspace_settings_test.py`, `workspaceOptions.test.ts`), plus a changelog entry under `apps/minds/changelog/`.
- It is filed and submitted per `.agents/skills/submit-upstream-changes/references/mngr-changes.md`, paired with the template's foundation PR.

### Shipped and user themes

- `system/themes/standard/theme.toml`: `contract = 2`, plus a description of the brand.
- `system/themes/mac-classic` and `system/themes/windows-2000`: run `migrate`, then hand-check `tokens.css` and `parts.css` (about 25 token uses each). Each sets its own roles and `--state-*` so it keeps today's look. `apps/chat.css` and `apps/getting-started.css` are checked for contract-1 names.
- Workspace themes under `themes/` (`WORKSPACE_THEMES_DIRECTORY`): a one-shot `system/scripts/env.d/` converge unit runs `workspace-themes migrate --all --source workspace`. It is idempotent, skips contract-2 folders, and commits nothing (themes are the user's).
- `/theme-gallery` (`system/apps/system_interface/frontend/theme-gallery.html` and `views/ThemeGallery.ts`): show the new parts and component batches as they land.

### Mechanical rename across apps (foundation PR)

- Every `--c-*` reference and every contract-1 Tailwind utility in:
  - `system/apps/{system_interface,chat,files,getting_started,routines,style_guide}/frontend/src`
  - `workspace_ui/src`
  - the theme folders
- Driven by `CONTRACT_1_RENAMES`, with a one-off script used only in that PR; the result is reviewed by hand.
- Chat's app-shaped tokens are mapped in `system/apps/chat/frontend/src/style.css` and its views:

| Contract 1 | Contract 2 |
|---|---|
| `--c-bg` | `--c-body-surface` |
| `--c-bg-sidebar`, `--c-surface-secondary` | `--c-body-surface-secondary` |
| `--c-surface`, `--c-bg-chat`, `--c-bg-composer` | `--c-body-surface-raised` |
| `--c-bg-code` | `--c-body-surface-secondary` |
| `--c-bg-user-bubble` | `--c-primary-subtle-surface` (see Open questions) |
| `--c-bubble-other` | `--c-body-surface-secondary` |
| `--c-text-primary` / `-secondary` / `-faint` | `--c-body-content` / `-secondary` / `-tertiary` |
| `--c-text-on-accent` | `--c-{role}-content` for the surface it sits on |
| `--c-border` / `-faint` / `-code` | `--c-border-default` / `-subtle` / `-subtle` |
| `--c-border-strong` | `--c-border-default` (fields take the studio-brand field recipe) |
| `--c-accent` / `-light` | `--c-accent-surface` / `-subtle-surface` |
| `--c-danger`, `--c-warning`, `--c-success` | `--c-{danger,warn,success}-surface` |
| `--c-danger-bg`, `--c-warning-bg`, `--c-success-bg` | `--c-{role}-subtle-surface` |
| `--c-danger-border`, `--c-success-border` | `--c-{role}-4` |
| `--c-stop-button` | `--c-inverse-surface` |
| `--c-step-done` / `-pending` | `--c-success-subtle-content` / `--c-neutral-6` |
| `--c-inverse-surface` | unchanged |
| every `-hover` token | dropped; the computed `-hover` utility instead |

- `system/apps/system_interface/frontend/style_guide.md`, `docs/system/themeable-apps.md`, the `create-theme` and `make-theme-icon` skills, and build-app's `references/frontend-choices.md` are rewritten to the role vocabulary, the brand fonts, and "hover is computed".
- `system_interface/.../theme_contract_test.py` keeps `contract.py` equal to `parts.ts` and the role tokens equal to `base.css`.

### Desktop chrome (system_interface)

- `views/Window.ts` and `views/TitleBar.ts`: focus-shaded title bar (`bg-body-surface-secondary` vs `-inactive`, icons at 50% when behind), border removed, control tooltips on the inverse surface.
- `views/Taskbar.ts` and `views/LauncherMenu.ts`: round 36px launcher field whose border turns accent when open, and taskbar entry states per the prototype. Tray untouched.
- `views/DesktopView.ts` and shortcut styling: flat shortcuts.
- `shell/wallpapers.py` and the bundled wallpapers: add the "studio" wordmark wallpaper (solid `--c-wallpaper-surface`, mask in `--c-wallpaper-mark`) as the default. A stored wallpaper choice is left as is.

### Shared components (`system/libs/workspace_ui/src/components`)

- Restyle existing components to studio-brand sizes:
  - `Button.ts`: add the `accent` variant, and the `sm`/`xs`/`icon`/`round`/`selected`/`quiet`/`block` options
  - `Input.ts`
  - `menu.ts`: card, row slab, divider
  - `dropdown.ts`
  - `Modal.ts`: dismiss on overlay mousedown only
  - `NoticeDialog.ts`: dismiss first, destructive as ghost-destructive
  - `segmentedControl.ts`
  - `toasts.ts`: tones
  - `hoverTooltip.ts`: 250ms intent delay, no fade
  - `Badge.ts`
  - `banner.ts`
- New (Mithril ports of the studio-brand components, each with `data-part` where the contract has a part):
  - Forms: `Switch`, `Checkbox`, `TextArea`, `FormLabel`, field error state on `Input`
  - Status: `Notice` (info/warn/success/error), `StatusBadge` pill, `Spinner` component, `HealthShape`
  - Surfaces: `Card`, `SectionHeader`, `Link`, `DialogCloseButton`
  - Overlays: `Overlay` (centered, anchored, drawer, bottom sheet), `Lightbox`, `Popover` and `Listbox` (keyboard-navigable, for mentions and pickers)
  - Data: `table` and `list-row` recipes (`list-row` is already a contract part)
- `src/themes/parts.ts` and `contract.py` `CORE_PARTS`: add only the parts a new component needs a theme to reach (for example `switch`, `checkbox`, `card`, `notice`). Each addition goes in both files and is checked by the parity test.

### App conversions (one PR per component batch)

Each app's feature-specific variables are remapped to role or ramp tokens in the PR that touches it.

- **Shell** (`system_interface`):
  - about 36 raw buttons → `Button`; `views/phone/Sheet.ts` → `Overlay` sheet; tray cards → `Popover`
  - `views/Overview.ts` white/black alpha utilities → inverse roles
  - appIcon fallback tile and character colors → ramp steps
- **Chat**:
  - `views/ModelProviderMenu.ts` and `modelProviderMenuStyles.ts` → `Menu`, `Switch`, `Button`
  - `views/MentionPopover.ts` → `Popover` and `Listbox`; `lightbox.ts` → `Lightbox`
  - `ToolChipGroup.ts` and `compaction-chips.ts` → `Badge`/`StatusBadge`
  - `accountRow.ts`, `message-renderers.ts` and `root/ChatRail.ts` → `Button`
  - `bg-gray-900` and `text-red-600` → roles; the `.terminal-placeholder` literals → inverse roles
- **Files**: remaining raw buttons and inputs; `--files-folder*` and `--files-family-*` → ramp steps (so themes and the workspace color reach them); `views/FileIcon.ts` SVG font and fill → tokens.
- **Getting started**: paging arrows → `Button`; `views/startSomething.ts` hues → ramp steps, still behind the `getting_started.start-glyph` part.
- **Routines**: raw inputs → `Input`/`Checkbox`/`Switch` as fits.
- **System Monitor**:
  - New `system/apps/system_monitor/frontend/` (Mithril/TS/Vite/Tailwind) in the `system/package.json` workspace, replacing `static/index.html`. Flask serves the build and calls `register_workspace_theme_route`.
  - `app.toml` gets `[theming] mode = "tokens"`.
  - Its tables, segmented ranges, tooltip, confirm row and stop buttons become `table`, `SegmentedControl`, `hoverTooltip`, `NoticeDialog` and `Button`.
  - `--ink-cpu`, `--ink-mem` and `--ink-disk` become ramp steps.
- **Style guide** (`system/apps/style_guide/frontend`): rebuild after `color-themes/src/styleGuide/{StyleGuideView,ColorGuideView,tokens}.ts`.
  - Ramps as strips; roles grouped by the labelled comments in `base.css`.
  - The type ramp, and every component in every variant and state.
  - A workspace-color preview picker that writes a page-local key override computed through a new `GET /api/workspace-color/preview?hex=` endpoint (the Python derivation), so the page never needs a second JS port.
  - The Theming view keeps its parts map and page-kit sample.
- **Screen, terminal, terminal_pty, datalib**: no changes beyond what the rename forces (screen and terminal use no contract color tokens today; terminal keeps `--term-*`).

### Changelog

- One `changelog/<branch>.md` per touched project: `workspace_themes`, `workspace_ui`, each app, `.agents/changelog/` for skill edits, `system/changelog/` for docs and themes.

## Implementation phases

**PR 1: foundation**

1. **Contract 2 and Standard values.**
   - `contract.py` groups and `CONTRACT_1_RENAMES`; the new `base.css` table, utilities and type ramp; bundled fonts; the page kit regenerated.
   - Mechanical rename across every app, `workspace_ui` and the shell's `default.css`; chat's token mapping.
   - Contract-parity test updated.
   - Result: every app renders on Courage keys with brand type. Components keep their current shapes.
2. **Theme ports and migration.** `migration.py`, `workspace-themes migrate`, validator rules, the env.d converge unit; Classic Mac and Windows 2000 ported and verified in the gallery. Result: all three shipped themes switch cleanly, and old user themes migrate on boot.
3. **Workspace color pipeline.** `workspace_color.py` (library and shell), the state file, the `workspace_color.css` route, `shell:theme` extension, `themeClient`/`themeBoot` link handling, the boot trigger, and the embedder-message handler; the paired Imbue Studio change sends `minds:workspace-color`. Result: a labelled workspace wears its color, and picking a new one in Imbue Studio recolors it at once.
4. **Desktop chrome and wallpaper.** Title bar, window, launcher field and shortcut states; the "studio" default wallpaper. Result: the desktop matches the prototype apart from the tray.
5. **Docs and skills.** style_guide.md, themeable-apps.md, create-theme, make-theme-icon and build-app references rewritten.

**Later PRs: component batches, each converting its adopters**

6. **Existing components restyled to studio-brand sizes.** Button `accent` variant, menu card, field, modal, tooltip and toast updates. Shell and chat raw buttons that need nothing new move to `Button` here.
7. **Forms.** `Switch`, `Checkbox`, `TextArea`, `FormLabel`, field errors. Adopters: chat's model provider menu and terminal view toggle, routines, files dialogs.
8. **Status.** `Notice`, `StatusBadge`, `Spinner`, `HealthShape`. Adopters: chat chips and notices, files notices, the shell's tray status.
9. **Surfaces and overlays.** `Card`, `SectionHeader`, `Link`, `DialogCloseButton`, `Overlay`, `Lightbox`, `Popover`/`Listbox`. Adopters: chat's mention popover and lightbox, the shell's phone sheet and tray cards, getting started.
10. **Data and System Monitor.** `table`/`list-row` recipes; System Monitor rebuilt on Mithril/Vite; files list rows.
11. **Style Guide rebuild.** Last, so it catalogues the finished set. Includes the workspace-color preview endpoint and picker.

Feature-specific variable remaps (`--files-*`, `--ink-*`, start-glyph hues, shell character and appIcon colors) ride with whichever of phases 6–10 first touches that app.

## Testing strategy

- **Color derivation** (`workspace_themes`, pure unit tests):
  - Golden keys for all ten Imbue Studio palette colors, generated once by running the prototype's `seedTheme.ts` and kept as a fixture; the Python port must match to a small OKLCH tolerance.
  - For any hex (property-style sample across hues and lightness), every derived key gives white text at least 4.5:1 and sits inside sRGB.
  - `harmonize` moves no status hue more than 15°.
  - `primary_content` reaches APCA Lc 60 or is pure black or white.
  - Courage and no-label both give an empty `workspace_color.css`.
- **Contract and validation:**
  - Parity between `contract.py`, `parts.ts` and `base.css` (extended existing test).
  - The validator rejects `-hover` tokens, contract-1 names and `contract = 1`.
- **Migration:**
  - Fixture contract-1 themes (tokens, parts, app overlays, dropped tokens) rewrite to the expected text.
  - Rerunning changes nothing; contract-2 folders are untouched.
  - Both shipped themes pass `validate` after migrating.
- **Shell:**
  - Label read from a fake `mngr list` result.
  - The state file write and revision bump.
  - `themes_changed` sent on a change.
  - `minds:workspace-color` triggers a reread and does not trust its body; an unknown or malformed payload is ignored.
- **Route:** `/_static/workspace_color.css` content, ETag and no-cache headers; a 304 on an unchanged revision.
- **Frontend unit (vitest):** `themeClient` keeps one workspace-color link ahead of the theme link and swaps it on revision change; `themeBoot` inserts it before first paint.
- **Browser tests** (marked `browser`):
  - Changing the state file plus a `shell:theme` recolors a framed app without reload.
  - First paint already has the workspace keys.
  - Classic Mac ignores the workspace color.
  - Every new component renders with its `data-part` and keyboard behavior (listbox arrows and Enter, overlay Escape and focus return, switch and checkbox toggling).
- **DOM measurement** (a headless preview, never screenshots): button heights and radii, field height and focus ring, menu card radius and row height, card radius, title bar height, measured against the studio-brand values.
- **Visual review:** before/after Playwright screenshots (Fortress) of every shipped app, the desktop, the theme gallery under all three themes, and two non-default workspace colors, collected for human review in each PR.
- **Gate:** `app-manifest select-tests --diff-base <base>` run in the background per PR; chat and shell suites with `-m ''` for the foundation PR.
- **Edge cases:**
  - Extreme colors: near-white, near-black, saturated yellow, saturated blue.
  - A malformed label.
  - A missing `mngr`.
  - A user theme with unknown `--theme-*` tokens.
  - A theme that references keys (opts into the workspace color).
  - Phone layout.
  - A browser without relative color syntax (see Open questions).

## Open questions

- **Default mismatch.** Imbue Studio's own default color is `confusion` (#0b292b), and it renders unlabelled workspaces that way. The workspace will show Courage for the same case, so Studio's chrome and the desktop would disagree for older workspaces. Should Studio backfill a label, or should the template default follow Studio?
- **The user's own chat bubble.** Today it is a green accent tint. `primary-subtle-surface` would make it follow the workspace color; `accent-subtle-surface` would keep it green-ish. Which?
- **Field borders.** Contract 1's `border-strong` (fields read as "type here") has no contract-2 role. Do fields use `border-default`, a ramp step (neutral 5), or a new `--c-border-strong` role?
- **Type ramp conflicts.** Studio-brand's ramp and color-themes' `typography.ts` ("no row step: dense chrome is body") may disagree on `--font-size-row`/`body-dense` and on button heights (32 vs 34px). These go to the user as they come up.
- **Relative color syntax floor.** The ramps and computed states need `oklch(from ...)` and CSS `pow()`. That means roughly Chrome/Edge 120+, Safari 16.4+ (fully reliable from about 18) and Firefox 128+. Imbue Studio's own Chromium is always past this. Only sharing-link visitors on old browsers fall below it: iOS 15 and earlier (iPhone 6s/7), Firefox ESR 115, or a Chromium that hasn't updated since late 2023. The current lean is to accept the floor. Exact versions to be confirmed before PR 1.
- **Imbue icon set.** It is deferred here (icons stay as they are). Should it be the next design-system PR after this plan?
- **Wallpaper key.** In `seedTheme` the wallpaper follows the primary, while the Imbue default uses Honey. Should a non-Courage color derive its wallpaper from the primary (as the prototype does) or from its own rotated hue?
- **Screen and terminal.** They stay on literal dark palettes. Should a follow-up move their chrome onto inverse roles so themes reach them?

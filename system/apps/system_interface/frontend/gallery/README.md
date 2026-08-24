# Component gallery + visual before/after harness

A static gallery of the System Interface's tokens and shared surfaces, plus a
Node tool that screenshots it and pixel-diffs two versions. Use it to prove a
CSS change renders identically (or to see exactly what moved). Built for the
design-system work in [`../../docs/design-system.md`](../../docs/design-system.md).

## Files

- `gallery.html` — static catalog. Every token swatch and component sits in a
  `<section data-shot="slug">` so it can be screenshot in isolation. Token
  sections (colour/type/spacing/radius/elevation) are read **live** from the
  compiled CSS, so a changed token value shows up automatically.
- `visual-diff.mjs` — the CLI (compile CSS with the Tailwind CLI → render the
  static gallery → screenshot each section → pixel-diff → HTML report).

## One-time setup

```
cd system/apps/system_interface/frontend
pnpm install
```

`node_modules` is gitignored, so this is needed once per checkout. The harness
drives the system Google Chrome via `playwright-core` (no browser download).

## Compare your working change against a baseline (the common case)

```
node gallery/visual-diff.mjs diff-refs main
open gallery/.visual-diff/report-main-vs-WORKING.html
```

`diff-refs <before-ref> [<after-ref|WORKING>]` extracts each ref's `style.css`
with `git show`, compiles and renders both, and writes a report with a
per-section verdict (`identical` / `differs (Npx)`), before/after thumbnails, and
a magenta diff overlay. No worktree is used — only the CSS varies, the gallery
markup is constant.

For a P1/P2 token sweep the whole report should read `identical`. Any section
that differs is either a real regression or an intended change to review.

## Manual two-capture flow (compare arbitrary states)

```
node gallery/visual-diff.mjs capture --label before        # captures the working style.css
# ...make changes...
node gallery/visual-diff.mjs capture --label after
node gallery/visual-diff.mjs compare before after
```

`capture --css <path>` points at a specific stylesheet instead of the working
`src/style.css`.

## Author the gallery directly

```
node gallery/visual-diff.mjs build-css --out gallery/compiled.css
open gallery/gallery.html
```

## Scope / caveats

- The token sections are faithful (read from the real compiled CSS). The
  component sections use the real class names but hand-written representative
  markup — they catch token/colour/spacing/shape regressions in shared surfaces,
  not per-screen layout. They are not a pixel replica of every view.
- Screenshots are deterministic on one machine (system fonts, animations frozen).
  Do not compare captures taken on different machines.
- Artifacts under `.visual-diff/`, `.vd-tmp/`, and `compiled.css` are gitignored.
- Prior art this mirrors: `system/vendor/mngr/apps/minds/scripts/visual_diff.py`.

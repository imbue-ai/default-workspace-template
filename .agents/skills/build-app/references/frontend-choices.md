# Frontend choices

Use these default choices unless the user has requested something different, or the specific task clearly requires a different choice.

## Overall design
Clean, modern, functional design. No unnecessary gimmicks.

## Font choices
* For all text: `font-family: ui-sans-serif, system-ui, sans-serif;`
* For code: `font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;`

## Color scheme
```css
:root {
  /* Status / feedback. One hue per semantic. These light values read on
     the white surface; .dark lifts each to a brighter value (below). */
  --c-important: #d90c00;
  --c-success: #0c8106;
  --c-warning: #b45300;
  --c-info: #166fc7;
  /* Warning surfaces (badge / notice backgrounds) use a yellow caution fill
     rather than a low-opacity tint of the brown-orange --c-warning, which
     reads muddy; the warning foreground stays --c-warning. */
  --c-warning-surface: hsl(49 100% 50% / 0.2);
  /* Notice / badge surfaces for the other semantics, derived as a faint tint of
     each hue. On white an 8% tint reads as a clear pale shape; .dark lifts the
     opacity (below) because the same tint over pure black is invisible. */
  --c-important-surface: rgb(from var(--c-important) r g b / 0.08);
  --c-success-surface: rgb(from var(--c-success) r g b / 0.08);
  --c-info-surface: rgb(from var(--c-info) r g b / 0.08);
  /* Accent / interactive (links, selected states, focus rings, progress). */
  --c-accent: #0069d9;
}
.dark {
  --c-accent: #4d9bff;
  --c-important: #fb1f13;
  --c-success: #12be09;
  --c-warning: #ff851a;
  --c-info: #4396ea;
  --c-important-surface: rgb(from var(--c-important) r g b / 0.22);
  --c-success-surface: rgb(from var(--c-success) r g b / 0.22);
  --c-info-surface: rgb(from var(--c-info) r g b / 0.22);
}
```

## Dark mode
Do NOT worry about implementing dark mode for initial UI mocks, unless the user requests it. You can include it for the final app.
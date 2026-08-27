// Single source of truth for every icon in the app.
//
// Icons are authored once here and consumed everywhere as SVG *strings*, which
// works for both rendering paths in this codebase: Mithril views wrap them with
// `m.trust(...)`, and the plain-DOM tab bar / lightbox assign them to
// `element.innerHTML`. Keeping icons as strings (rather than Mithril vnodes)
// is what lets a single definition serve both.
//
// The stroke-outline icons share one Feather/Lucide-style frame (24x24 grid,
// no fill, round-capped `currentColor` strokes); only their inner path markup
// differs, so it lives in STROKE_PATHS and `icon()` wraps it. Filled or
// otherwise non-standard glyphs (stop, warning, the Claude logo, the progress
// status badges, the login spinner) have their own builders below.

const XMLNS = "http://www.w3.org/2000/svg";

// Inner markup for stroke-outline icons, all drawn on a 24x24 grid.
const STROKE_PATHS = {
  attach:
    '<path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/>',
  // The single canonical "x" -- previously re-authored in four places (the
  // attachment-remove chip, the tab close button, the login modal, the image
  // lightbox).
  close: '<path d="M18 6L6 18"/><path d="M6 6l12 12"/>',
  // Closing a tab and taking its object off the machine are different acts, so
  // they get different glyphs: a minus puts the tab away and leaves the thing
  // running, an "x" ends it everywhere.
  minus: '<path d="M6 12h12"/>',
  // A third act between those two, and it gets a third glyph: taking an object
  // out of ONE project. The bare minus is already "put the tab away", and the
  // object here keeps running and stays in Everything, so neither that nor the
  // "x" says it. The ring is what marks it as acting on the filing rather than
  // on the tab.
  "minus-circle": '<circle cx="12" cy="12" r="9"/><path d="M8 12h8"/>',
  file: '<path d="M14 3v4a1 1 0 0 0 1 1h4"/><path d="M17 21H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h7l5 5v11a2 2 0 0 1-2 2z"/>',
  // Up-arrow, shared by the composer "send" button and the pending-message
  // "interrupt and send now" action.
  send: '<path d="M12 19V5"/><path d="M5 12l7-7 7 7"/>',
  trash:
    '<polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>',
  // Lucide "user-plus", on the Share rows: sharing grants people access by
  // email, so the glyph shows a person being added.
  "user-plus":
    '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M19 8v6"/><path d="M22 11h-6"/>',
  // Lucide "power": the classic power-button glyph, on the destructive
  // "Quit {app}" row for a registered app.
  power: '<path d="M12 2v10"/><path d="M18.4 6.6a9 9 0 1 1-12.77.04"/>',
  refresh:
    '<polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>',
  download:
    '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>',
  // The single canonical checkmark, shared by the login "success" state and the
  // permission "granted" verdict.
  check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
  // The key that heads a permission request, and the cube that stands in for a
  // service with no bundled brand mark. Both are lucide (`key-round`, `box`) --
  // the same two glyphs the minds app draws on its own permission surfaces, so
  // the in-chat card and the review popup agree.
  key: '<path d="M2.586 17.414A2 2 0 0 0 2 18.828V21a1 1 0 0 0 1 1h3a1 1 0 0 0 1-1v-1a1 1 0 0 1 1-1h1a1 1 0 0 0 1-1v-1a1 1 0 0 1 1-1h.172a2 2 0 0 0 1.414-.586l.814-.814a6.5 6.5 0 1 0-4-4z"/><circle cx="16.5" cy="7.5" r=".5" fill="currentColor"/>',
  box: '<path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"/><path d="m3.3 7 8.7 5 8.7-5"/><path d="M12 22V12"/>',
  // Exclamation mark used for the permission "couldn't complete" verdict.
  alert: '<path d="M12 6v7"/><path d="M12 17.5h0"/>',
  "chevron-down": '<path d="M6 9l6 6 6-6"/>',
  "chevron-right": '<path d="M9 6l6 6-6 6"/>',
  // The back affordance in the provider chooser -- the mirror of chevron-right.
  "chevron-left": '<path d="M15 6l-6 6 6 6"/>',
  // Lightning bolt for the composer fast-mode toggle.
  zap: '<path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/>',
  // Magnifying glass for the model-search box.
  search: '<circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/>',
  "external-link":
    '<path d="M14 4h6v6"/><path d="M20 4l-9 9"/><path d="M19 13v6a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h6"/>',
  // Pencil, on the tab menu's Rename row and the project switcher's per-row
  // settings control.
  edit: '<path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5z"/>',
  // Lucide "folder-plus", on the object menu's "Add to project..." row.
  "folder-plus":
    '<path d="M12 10v6"/><path d="M9 13h6"/><path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>',
  // Gear, on the switcher's current-project row: the project you are already in
  // does not need to be offered again, so that row leads to its settings.
  settings:
    '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
} as const;

export type IconName = keyof typeof STROKE_PATHS;

export interface IconOptions {
  // Pixel size. Omit to let CSS size the svg (used by the tab-bar buttons).
  size?: number;
  strokeWidth?: number;
  className?: string;
  // Draw the glyph solid instead of as an outline. Only sensible for shapes that read as a
  // silhouette -- the composer chip's "fast mode" bolt, which has to say "on" at 12px.
  filled?: boolean;
}

/** Full <svg> string for a stroke-outline icon. */
export function icon(name: IconName, opts: IconOptions = {}): string {
  const dims = opts.size === undefined ? "" : ` width="${opts.size}" height="${opts.size}"`;
  const cls = opts.className ? ` class="${opts.className}"` : "";
  const strokeWidth = opts.strokeWidth ?? 2;
  const paint = opts.filled ? 'fill="currentColor" stroke="none"' : 'fill="none" stroke="currentColor"';
  return (
    `<svg xmlns="${XMLNS}"${cls}${dims} viewBox="0 0 24 24" ${paint} ` +
    `stroke-width="${strokeWidth}" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">` +
    `${STROKE_PATHS[name]}</svg>`
  );
}

/** Solid square "stop / interrupt" glyph (filled, not stroked). */
export function stopIcon(size = 14): string {
  return (
    `<svg xmlns="${XMLNS}" width="${size}" height="${size}" viewBox="0 0 24 24" ` +
    `fill="currentColor" stroke="none" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>`
  );
}

/** Circular warning badge (outlined circle + exclamation). */
export function warningIcon(size = 26): string {
  return (
    `<svg xmlns="${XMLNS}" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" aria-hidden="true">` +
    `<circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="2"/>` +
    `<path d="M12 8v4.5" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/>` +
    `<circle cx="12" cy="16" r="0.9" fill="currentColor"/></svg>`
  );
}

/** Animated login spinner (carries the `.claude-login-spinner` CSS hook). */
export function loginSpinnerIcon(): string {
  return (
    `<svg xmlns="${XMLNS}" class="claude-login-spinner" viewBox="0 0 24 24" fill="none" aria-hidden="true">` +
    `<circle cx="12" cy="12" r="10" stroke="currentColor" stroke-opacity="0.18" stroke-width="3"/>` +
    `<path d="M22 12a10 10 0 0 1-10 10" stroke="currentColor" stroke-width="3" stroke-linecap="round"/></svg>`
  );
}

// ── Progress-view status badges (16x16 grid, carry their own `.pv-icon` hooks) ──

/** Completed step: filled disc with a white check. */
export function statusDoneIcon(): string {
  return (
    `<svg xmlns="${XMLNS}" class="pv-icon pv-icon--done" width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">` +
    `<circle cx="8" cy="8" r="7" fill="currentColor"/>` +
    `<path d="M4.5 8L7 10.5L11.5 6" stroke="white" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>`
  );
}

/** Active-but-settled step: a static partial ring. */
export function statusRingIcon(): string {
  return (
    `<svg xmlns="${XMLNS}" class="pv-icon pv-icon--in-flight" width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">` +
    `<circle cx="8" cy="8" r="6" stroke="currentColor" stroke-width="1.5" opacity="0.35"/>` +
    `<path d="M8 2 A6 6 0 0 1 14 8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>`
  );
}

/** Pending step: a dashed circle outline. */
export function statusPendingIcon(): string {
  return (
    `<svg xmlns="${XMLNS}" class="pv-icon pv-icon--pending" width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">` +
    `<circle cx="8" cy="8" r="6.5" stroke="currentColor" stroke-width="1" stroke-dasharray="2 2"/></svg>`
  );
}

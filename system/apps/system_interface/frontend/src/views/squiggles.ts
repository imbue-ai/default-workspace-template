/**
 * The hand-drawn "squiggle" glyphs that identify a project.
 *
 * Both the glyph paths and their bounding boxes are copied verbatim from the
 * minds-dockview design prototype's bundle -- each path is drawn in its own
 * arbitrary coordinate space, so the box is the only thing that says where the
 * ink actually is. `squiggleMarkup` derives a square viewBox and a stroke width
 * from that box, and the constants in that math (the 0.9 fill fraction, the
 * 0.085 stroke ratio) are calibrated against the prototype -- changing them
 * makes the glyphs render at a visibly different weight or crop.
 *
 * Two glyphs here are not one of the ten: the composite (five squiggles
 * clustered together, which is Everything's identity, not a project's) and the
 * monogram tile a project falls back to when it has no glyph to draw.
 *
 * Like `icons.ts`, glyphs are produced as SVG *strings* so they work with both
 * rendering paths in this codebase (`m.trust(...)` in Mithril views, and
 * `element.innerHTML` in the plain-DOM tab bar).
 */

const XMLNS = "http://www.w3.org/2000/svg";

export interface SquiggleGlyph {
  path: string;
  // [x, y, width, height] of the path's ink in its own coordinate space.
  box: [number, number, number, number];
  // The glyph's signature color, used when the caller doesn't override it.
  color: string;
}

export const SQUIGGLE_GLYPHS: readonly SquiggleGlyph[] = [
  {
    color: "#F0603A",
    box: [37.176, 48.3799, 273.648, 251.241],
    path: "M135.645 183.274C136.308 239.581 113.099 338.211 53.1933 276.516C8.86628 230.865 93.2577 183.274 135.645 183.274ZM135.645 183.274C135.38 160.824 127.299 126.496 144.73 114.095C221.976 59.14 261.756 41.4133 295.861 104.661C334.561 176.43 275.766 214.855 111.853 176.43C-46.3912 139.335 107.779 -33.641 175.376 104.661C226.603 209.471 297.257 191.204 288.466 256.259C277.972 333.904 177.018 291.268 135.645 183.274Z",
  },
  {
    color: "#16A34A",
    box: [22.6105, 51.7414, 302.778, 244.518],
    path: "M148.404 209.883C102.469 296.155 -4.33442 280.508 34.4253 209.883C70.2883 144.536 139.03 227.502 205.868 217.365C272.706 207.229 367.495 55.2416 296.971 55.2414C251.031 55.2413 211.028 111.125 190.718 139.42M148.404 209.883C184.098 165.842 216.77 174.879 238.783 184.947C284.819 206.003 262.933 286.641 213.941 292.583C164.95 298.525 117.565 152.301 145.209 94.6285C167.324 48.4904 205.868 81.0468 190.718 139.42M148.404 209.883C152.382 199.077 175.423 160.741 190.718 139.42",
  },
  {
    color: "#E3A400",
    box: [11.4073, 42.7764, 325.185, 262.445],
    path: "M63.4137 126.468C95.5831 109.125 166.848 93.3957 180.329 176.452C197.179 280.272 136.428 328.313 116.756 286.888C82.5439 214.845 136.415 33.6696 205.605 46.9738C274.795 60.278 217.604 195.459 229.349 231.57C241.093 267.682 360.835 186.669 327.134 148.181C293.432 109.694 203.052 165.524 144.84 191.42C86.629 217.316 20.105 213.33 15.3819 188.712C11.5531 168.756 31.2443 143.811 63.4137 126.468Z",
  },
  {
    color: "#45BC4E",
    box: [20.3717, 63.5056, 307.257, 220.989],
    path: "M206.446 90.9767C243.597 129.725 203.609 214.813 127.326 206.001C17.0294 193.26 68.7492 70.199 144.155 126.063C212.883 176.98 194.856 186.434 280.008 185.254C365.161 184.074 330.801 365.266 144.155 224.308C-17.6754 102.09 -34.2289 394.251 206.446 228.896C410.295 88.8424 277.127 29.9554 206.446 90.9767Z",
  },
  {
    color: "#12B5A5",
    box: [36.0345, 31.7046, 275.349, 284.16],
    path: "M55.3134 104.77C105.419 114.914 239.46 176.683 278.311 228.815C326.875 293.98 311.017 329.374 264.105 304.235C217.193 279.095 137.244 202.683 187.13 133.217C239.865 59.7825 343.723 130.24 282.276 146.78C223.374 162.634 75.4657 141.156 57.2956 176.881C39.1254 212.606 215.937 226.83 216.203 168.612C216.468 110.393 149.534 52.836 40.1165 35.6351L55.3134 104.77Z",
  },
  {
    color: "#17A2C4",
    box: [37.1765, 37.1774, 273.647, 273.646],
    path: "M140.083 186.307C111.755 139.377 60.4901 62.5552 145.011 43.7673C243.922 21.7807 150.94 121.392 261.522 206.62C355.518 279.065 263.634 330.598 228.434 296.627C204.122 273.163 161.98 222.583 140.083 186.307ZM140.083 186.307C66.5152 64.4302 -4.84772 274.739 77.4276 263.356C163.496 251.449 363.743 201.379 292.145 103.305C268.377 70.7465 230.703 87.9462 140.083 186.307Z",
  },
  {
    color: "#3B82F6",
    box: [43.8991, 46.1425, 260.204, 255.718],
    path: "M110.481 265.303C194.519 261.66 247.893 209.286 208.963 117.725M208.963 117.725C117.429 56.8824 32.4037 82.0244 49.6384 172.511C84.6559 356.361 439.631 339.341 208.963 117.725ZM208.963 117.725C153.813 5.48537 347.054 51.2431 208.963 117.725ZM208.963 117.725C101.362 158.319 186.64 230.238 268.531 194.016C339.835 156.921 279.87 104.551 208.963 117.725Z",
  },
  {
    color: "#7C5CFF",
    box: [40.538, 42.219, 266.924, 263.562],
    path: "M177.893 170.868C136.106 150.531 114.42 184.404 108.8 203.882H303.962V285.428C286.221 293.132 193.232 281.004 177.893 170.868ZM177.893 170.868C239.497 203.882 283.011 160.141 264.952 127.457C252.366 104.677 207.099 93.7821 177.893 170.868ZM177.893 170.868C176.46 133.891 174.701 29.4034 105.608 47.892C40.7496 65.2473 64.2843 134.06 132.072 170.868M132.072 170.868C198.574 206.977 177.894 316.301 98.0034 300.784C9.73549 283.64 35.8531 158.821 132.072 170.868Z",
  },
  {
    color: "#B455E8",
    box: [11.4081, 49.5007, 325.193, 248.999],
    path: "M162.009 149.372C197.084 164.298 232.01 184.257 249.299 188.998C321.705 208.849 364.493 147.354 304.991 99.0124C257.389 60.3393 192.631 104.653 162.831 148.529C162.564 148.784 162.29 149.066 162.009 149.372ZM162.009 149.372C139.382 139.744 116.694 132.211 98.6386 132.211C21.2559 132.211 -19.4881 195.987 52.0329 228.02C112.727 255.204 146.913 165.869 162.009 149.372ZM204.739 53.0007C194.774 64.6492 155.575 130.97 159.899 187.833C165.613 262.966 250.179 332.857 271.576 270.828C284.554 233.207 194.863 214.221 227.023 143.568C247.834 97.8476 265.707 71.3471 281.828 53.0008L204.739 53.0007ZM164.295 149.975C145.047 147.062 53.2937 209.674 62.0867 294.999H126.572C130.382 263.937 161.246 149.975 164.295 149.975Z",
  },
  {
    color: "#EC4899",
    box: [26.533, 27.0939, 294.935, 293.812],
    path: "M159.369 114.756C196.953 103.845 197.425 34.45 107.939 30.6785C-3.91736 25.9641 90.4208 219.44 265.756 47.0711C333.67 70.1423 364.301 152.419 172.34 182.248C-67.6106 219.535 261.104 441.538 200.124 221.249C139.144 0.95926 429.056 257.706 132.337 242.926C-59.5395 233.368 59.1466 140.842 159.369 114.756Z",
  },
];

/**
 * Full <svg> string for one squiggle, sized to `size` pixels square.
 *
 * `glyphIndex` wraps, so any integer (including a negative or an out-of-range
 * one persisted by an older client) resolves to a real glyph. Pass a `color` to
 * override the glyph's own signature color; pass null to keep it.
 */
export function squiggleMarkup(glyphIndex: number, color: string | null, size: number): string {
  const count = SQUIGGLE_GLYPHS.length;
  const glyph = SQUIGGLE_GLYPHS[((glyphIndex % count) + count) % count];
  const [x, y, w, h] = glyph.box;
  // Square the box up and leave a 10% margin, so glyphs of different aspect
  // ratios all read as the same visual weight at the same rendered size.
  const side = Math.max(w, h) / 0.9;
  const viewBox = `${x + w / 2 - side / 2} ${y + h / 2 - side / 2} ${side} ${side}`;
  return (
    `<svg xmlns="${XMLNS}" width="${size}" height="${size}" viewBox="${viewBox}" fill="none" ` +
    `stroke="${color ?? glyph.color}" stroke-width="${side * 0.085}" stroke-linecap="round" ` +
    `stroke-linejoin="round" aria-hidden="true"><path d="${glyph.path}"/></svg>`
  );
}

// The composite glyph's geometry, copied verbatim from the prototype bundle
// along with the sprite table below. Every number is calibrated against those
// glyphs rather than derived, so changing one re-cuts the whole cluster: the
// canvas the sprites are laid out on, the stroke weight they are drawn at in
// canvas units, how far the layout pushes them apart, and the point they are
// spread around.
const COMPOSITE_CANVAS = 100;
const COMPOSITE_STROKE_BASE = 6.5;
const COMPOSITE_SPREAD = 1.35;
const COMPOSITE_ORIGIN = { x: 52, y: 51 };

interface CompositeSprite {
  // Index into SQUIGGLE_GLYPHS.
  glyph: number;
  // Where the sprite sits, in the pre-spread layout space COMPOSITE_ORIGIN is
  // measured in.
  cx: number;
  cy: number;
  // The side length the sprite's ink is scaled to fill, before the spread.
  target: number;
  // Degrees, applied about the sprite's own center.
  rot: number;
}

const COMPOSITE_SPRITES: readonly CompositeSprite[] = [
  { glyph: 0, cx: 40, cy: 42, target: 46, rot: -10 },
  { glyph: 3, cx: 62, cy: 40, target: 46, rot: 20 },
  { glyph: 5, cx: 44, cy: 62, target: 46, rot: -30 },
  { glyph: 7, cx: 64, cy: 60, target: 46, rot: 12 },
  { glyph: 9, cx: 52, cy: 51, target: 46, rot: 0 },
];

/**
 * Full <svg> string for the composite glyph, sized to `size` pixels square.
 *
 * This is Everything's identity rather than any project's, so it takes no color
 * override: each sprite keeps its own signature color, and the multicolor
 * cluster is what reads as "all of the projects at once".
 */
export function compositeSquiggleMarkup(size: number): string {
  const half = COMPOSITE_CANVAS / 2;
  const sprites = COMPOSITE_SPRITES.map((sprite) => {
    const glyph = SQUIGGLE_GLYPHS[sprite.glyph];
    const [x, y, w, h] = glyph.box;
    // Each path is drawn in its own coordinate space, so the sprite is moved
    // into place by its ink's center: scale about that center, then translate
    // it to the spread position.
    const inkX = x + w / 2;
    const inkY = y + h / 2;
    const tx = half + (sprite.cx - COMPOSITE_ORIGIN.x) * COMPOSITE_SPREAD;
    const ty = half + (sprite.cy - COMPOSITE_ORIGIN.y) * COMPOSITE_SPREAD;
    const scale = (sprite.target * COMPOSITE_SPREAD) / Math.max(w, h);
    // The stroke is scaled with everything else, so it has to be pre-divided
    // for all five sprites to end up at the same rendered weight.
    const strokeWidth = COMPOSITE_STROKE_BASE / scale;
    return (
      `<g transform="translate(${tx} ${ty}) rotate(${sprite.rot}) scale(${scale}) ` +
      `translate(${-inkX} ${-inkY})"><path d="${glyph.path}" stroke="${glyph.color}" ` +
      `stroke-width="${strokeWidth}" stroke-linecap="round" stroke-linejoin="round"/></g>`
    );
  });
  return (
    `<svg xmlns="${XMLNS}" width="${size}" height="${size}" ` +
    `viewBox="0 0 ${COMPOSITE_CANVAS} ${COMPOSITE_CANVAS}" fill="none" aria-hidden="true">` +
    `${sprites.join("")}</svg>`
  );
}

// The CIE L* at which black ink stops beating white ink on a tile of that
// lightness. Mirrors the minds chrome's titlebar recipe
// (`lch(from var(--titlebar-bg) calc((49.44 - l) * infinity) 0 0)` in the
// shell's static/app.css), so a project's tile and the window chrome around it
// flip at exactly the same point.
const CONTRAST_LIGHTNESS_THRESHOLD = 49.44;

const HEX_COLOR_PATTERN = /^#?([0-9a-f]{3}|[0-9a-f]{6})$/i;

/** CIE L* of a `#rgb` / `#rrggbb` color, or null if it is not one of those. */
function hexLightness(color: string): number | null {
  const match = HEX_COLOR_PATTERN.exec(color.trim());
  if (match === null) return null;
  const body = match[1].length === 3 ? [...match[1]].map((digit) => digit + digit).join("") : match[1];
  const channels = [0, 2, 4].map((offset) => parseInt(body.slice(offset, offset + 2), 16) / 255);
  const linear = channels.map((channel) =>
    channel <= 0.04045 ? channel / 12.92 : Math.pow((channel + 0.055) / 1.055, 2.4),
  );
  const luminance = 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2];
  // The CIE lightness curve, with its linear segment for near-black values.
  return luminance > 216 / 24389 ? 116 * Math.cbrt(luminance) - 16 : (24389 / 27) * luminance;
}

/**
 * Full <svg> string for a project's monogram, sized to `size` pixels square:
 * the first letter of its name on a rounded tile painted in its color.
 *
 * This is the fallback for a project with no glyph to draw, so it has to work
 * against any color the user picked -- the letter flips black or white off the
 * tile's lightness, the same self-theming math the titlebar does in CSS. A
 * color that is not a hex literal cannot be measured, and falls back to the
 * black letter that CSS would land on for the same reason.
 */
export function monogramMarkup(name: string, color: string, size: number): string {
  const lightness = hexLightness(color);
  const ink = lightness !== null && lightness < CONTRAST_LIGHTNESS_THRESHOLD ? "#ffffff" : "#000000";
  // Project names are user text, so the letter is escaped before it lands in
  // markup that callers hand to `m.trust` / `innerHTML`.
  const initial = name
    .trim()
    .charAt(0)
    .toUpperCase()
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
  return (
    `<svg xmlns="${XMLNS}" width="${size}" height="${size}" viewBox="0 0 100 100" aria-hidden="true">` +
    `<rect width="100" height="100" rx="22" fill="${color}"/>` +
    `<text x="50" y="50" fill="${ink}" font-size="56" font-weight="600" text-anchor="middle" ` +
    `dominant-baseline="central">${initial}</text></svg>`
  );
}

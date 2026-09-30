/**
 * An app's own icon, made safe to inline, and the fallback rule for apps
 * without one.
 *
 * An app registers its icon as SVG markup (`forward_port.py --icon`), the
 * registry carries it verbatim on the app's row, and the server hands it to
 * this UI on `AppEntry.icon`. That markup is authored by a skill, so it is
 * untrusted: every surface that draws an app goes through
 * `appIconMarkup`/`appIconMarkupForApp` here, and nothing inlines a registry
 * string on its own.
 *
 * The gate is `sanitizeIconMarkup`, and it is deliberately the only one:
 *
 * - Parsing is DOMPurify's, not ours. Hand-rolling HTML validation is how
 *   mutation-XSS gets in; DOMPurify parses the markup with the browser's own
 *   parser and keeps only an SVG allowlist.
 * - Anything that can execute or reach off the page is refused rather than
 *   repaired at the edges: elements that run code, navigate, load a resource,
 *   embed foreign HTML, or retarget an attribute at runtime, `<style>` and
 *   `style=` (CSS can fetch), `on*` handlers, and any URI that is not a
 *   `#fragment` inside the icon itself.
 * - `class` goes too. With no stylesheet of its own an icon's classes can only
 *   match the workspace's, which is a way to borrow layout rules rather than a
 *   way to draw.
 * - The result must be exactly one `<svg>` element. Two roots, a text node
 *   beside the root, or anything that is not an `<svg>` is not an icon, and is
 *   rejected whole -- the caller then draws its built-in glyph.
 *
 * This is defense in depth, not the only defense: `forward_port.py` validates
 * on the way into the registry. This gate is the last one because it is the
 * one that runs against the DOM the markup is about to enter.
 *
 * Sizing and color live here too, since both are decided from the same parsed
 * tree. The icon is rendered at the caller's pixel size on the caller's own
 * grid (its `viewBox` is kept, or derived from its width/height, so the art
 * scales rather than crops), and an icon that names no root `fill` inherits
 * `currentColor` the way the built-in glyphs do -- while one that paints
 * itself keeps every color it asked for.
 */

import DOMPurify from "dompurify";
import type { AppRecord } from "../../model/records";

const SVG_NAMESPACE = "http://www.w3.org/2000/svg";

// The same cap `forward_port.py` (MAX_ICON_LENGTH) enforces. Repeated here
// because this module is the last thing between the markup and the DOM, and it
// must not depend on the registration having run.
export const MAX_ICON_LENGTH = 16384;

// Elements refused outright. Everything here either runs code, navigates,
// loads a resource, embeds foreign content, or can retarget an attribute after
// the sanitizer has looked at it.
const FORBIDDEN_TAGS: readonly string[] = [
  "script",
  "style",
  "a",
  "image",
  "iframe",
  "foreignobject",
  "animate",
  "animatemotion",
  "animatetransform",
  "set",
  "handler",
];

// Attributes refused by name. `style` is here because CSS can fetch (a
// `url(...)` in a declaration is a request), and the icon has presentation
// attributes for everything it legitimately needs to say.
const FORBIDDEN_ATTRIBUTES: readonly string[] = ["style", "class"];

// Attributes that name a resource. Only a same-document `#fragment` is kept,
// so an icon can reference its own gradient and nothing else.
const REFERENCE_ATTRIBUTES: ReadonlySet<string> = new Set(["href", "xlink:href", "src", "xml:base"]);

// What DOMPurify will accept as an attribute VALUE. It tests this against
// every attribute, not only the ones that name a resource, so it has to admit
// ordinary values (path data, transforms, colors) as well. This is its stock
// expression with the schemes taken out: a value carrying any scheme at all --
// `https:`, `data:`, `javascript:` -- is refused, and a scheme-less value or a
// `#fragment` into the icon itself is kept.
const SCHEMELESS_OR_FRAGMENT_URI = /^(?:#|[^a-z]|[a-z+.-]+(?:[^a-z+.:-]|$))/i;

// What THIS module will accept in an attribute that names a resource: a
// fragment into the icon and nothing else. Stricter than the above on purpose
// -- DOMPurify's expression has to let a relative URL through to stay usable
// for ordinary values, and an icon has no business fetching one.
const FRAGMENT_ONLY_URI = /^#[\w.:-]*$/;

// The opening tag an icon has to start with. Checked on the raw string before
// anything is parsed: sanitizing `<div><svg/></div>` would leave a perfectly
// good svg behind, and drawing that would mean drawing something the author
// did not write. An icon is one `<svg>` element, and markup that is not one is
// refused rather than repaired.
const SVG_OPENING_TAG = /^<svg[\s>]/i;

// A `url(...)` paint/filter reference that points anywhere but into this icon.
const EXTERNAL_URL_REFERENCE = /url\(\s*['"]?(?!#)/i;

// `javascript:` with the whitespace and control characters a parser ignores
// stripped out, so `java\nscript:` is caught with the plain spelling.
const JAVASCRIPT_URI = /javascript:/i;

// How many sanitized icons to remember. Every redraw of the rail re-renders
// each row, and re-parsing a handful of icons on each one is pure waste; a
// machine never has enough distinct apps to approach the cap, and blowing it
// away wholesale (rather than evicting one entry) keeps the bookkeeping to one
// line.
const MAX_CACHE_ENTRIES = 64;
const sanitizedByKey = new Map<string, string | null>();

/** Drop the whitespace and control characters a URL parser ignores, so an
 *  obfuscated scheme (a `javascript:` with a newline inside it) is compared
 *  in its plain spelling. */
function collapsed(value: string): string {
  let result = "";
  for (const character of value) {
    if (character.charCodeAt(0) > 0x20) result += character;
  }
  return result;
}

/** FNV-1a, as an id prefix that is stable per icon.
 *
 *  Two apps may both ship a gradient called "a", and inlining both would leave
 *  the second one's shapes painted from the first one's gradient -- ids are
 *  document-wide. Prefixing every id (and every reference to it) with a hash of
 *  the icon separates them, while keeping the same icon's markup identical
 *  wherever it is drawn, which is what a random prefix would not do. */
function iconIdPrefix(markup: string): string {
  let hash = 0x811c9dc5;
  for (let index = 0; index < markup.length; index += 1) {
    hash ^= markup.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return `app-icon-${hash.toString(36)}-`;
}

/** The one element child of `fragment`, or null unless it is exactly one
 *  element with nothing beside it. */
function onlyElementChild(fragment: DocumentFragment): Element | null {
  const children = Array.from(fragment.childNodes);
  const elements = children.filter((node): node is Element => node.nodeType === Node.ELEMENT_NODE);
  if (elements.length !== 1) return null;
  const hasStrayText = children.some((node) => node.nodeType === Node.TEXT_NODE && node.textContent?.trim() !== "");
  return hasStrayText ? null : elements[0];
}

/**
 * Drop every attribute that can execute or reach off the page.
 *
 * DOMPurify has already been asked for all of this through its config; this
 * runs anyway, because the rules are the point and reading them at the DOM
 * they apply to is the only way to be sure of them.
 */
function scrubAttributes(root: Element): void {
  for (const element of [root, ...Array.from(root.querySelectorAll("*"))]) {
    for (const attribute of Array.from(element.attributes)) {
      const name = attribute.name.toLowerCase();
      const value = collapsed(attribute.value);
      const isUnsafe =
        name.startsWith("on") ||
        FORBIDDEN_ATTRIBUTES.includes(name) ||
        JAVASCRIPT_URI.test(value) ||
        EXTERNAL_URL_REFERENCE.test(value) ||
        (REFERENCE_ATTRIBUTES.has(name) && !FRAGMENT_ONLY_URI.test(value));
      if (isUnsafe) element.removeAttribute(attribute.name);
    }
  }
}

/** Rewrite every id in the icon, and every reference to one, behind `prefix`. */
function namespaceIds(root: Element, prefix: string): void {
  const elements = [root, ...Array.from(root.querySelectorAll("*"))];
  const renamed = new Map<string, string>();
  for (const element of elements) {
    const id = element.getAttribute("id");
    if (id !== null && id !== "") renamed.set(id, `${prefix}${id}`);
  }
  if (renamed.size === 0) return;
  for (const element of elements) {
    for (const attribute of Array.from(element.attributes)) {
      if (attribute.name.toLowerCase() === "id") {
        const rename = renamed.get(attribute.value);
        if (rename !== undefined) element.setAttribute(attribute.name, rename);
        continue;
      }
      let value = attribute.value;
      for (const [id, rename] of renamed) {
        if (value === `#${id}`) value = `#${rename}`;
        value = value.split(`url(#${id})`).join(`url(#${rename})`);
      }
      if (value !== attribute.value) element.setAttribute(attribute.name, value);
    }
  }
}

/** The tile every icon is drawn on: `docs/system/app-icons.md`, 216 square with a 32 per cent
 *  corner, and a 144 box centred in it for the mark. The tile is authored at 216 and drawn at
 *  whatever pixel size the caller asks for. */
const TILE_SIZE = 216;
const TILE_RADIUS = 69.12;
const GLYPH_BOX = 144;
const GLYPH_INSET = (TILE_SIZE - GLYPH_BOX) / 2;

// The pair the shell paints when an app brought no tile of its own: under the monogram of an app
// with no icon, and under a line glyph drawn before these rules. One pair for both, so the two
// read as the same kind of thing -- an app the workspace had to draw for.
//
// From the palette in `docs/system/app-icons.md`. It was the board's most neutral pair, on the
// reasoning that a letter an app wears for want of an identity should not claim a louder one --
// but that neutral is the arcs wallpaper's own colour, so the tile vanished into the backdrop and
// left the letter and the shadow standing for the app alone.
const FALLBACK_TILE_BACKGROUND = "#F5D6A0";
const FALLBACK_TILE_INK = "#492222";

/** Numeric length attribute (`24`, `24px`), or null when it says something
 *  this cannot turn into a viewBox. */
function lengthAttribute(root: Element, name: string): number | null {
  const raw = root.getAttribute(name);
  if (raw === null) return null;
  const parsed = Number.parseFloat(raw);
  if (!Number.isFinite(parsed) || parsed <= 0) return null;
  return parsed;
}

/**
 * Put the icon on the caller's grid: `size` pixels square, scaling rather than
 * cropping, and inheriting `currentColor` unless it paints itself.
 *
 * An icon with no `viewBox` has its own width and height as one, so replacing
 * those with the requested size scales the art instead of showing a corner of
 * it. An icon with neither is not drawable at a size we choose, and is
 * rejected.
 */
function normalizeRoot(root: Element, sizePx: number): boolean {
  if (root.getAttribute("viewBox") === null) {
    const width = lengthAttribute(root, "width");
    const height = lengthAttribute(root, "height");
    if (width === null || height === null) return false;
    root.setAttribute("viewBox", `0 0 ${width} ${height}`);
  }
  root.setAttribute("width", String(sizePx));
  root.setAttribute("height", String(sizePx));
  // Decoration beside a label that already names the app, exactly like the
  // built-in glyphs, and never a tab stop.
  root.setAttribute("aria-hidden", "true");
  root.setAttribute("focusable", "false");
  // A monochrome icon says nothing about fill and takes the text color it is
  // drawn beside; one that paints itself has its own root fill (or paints on
  // its shapes, which this never touches) and keeps every color it asked for.
  if (root.getAttribute("fill") === null) root.setAttribute("fill", "currentColor");
  return true;
}

/**
 * Whether the icon brings colour of its own, rather than taking the surface's.
 *
 * A tile built to `docs/system/app-icons.md` carries a filled background shape. The line glyphs
 * that predate those rules carry no fill at all and take `currentColor` from the text beside
 * them, which is exactly what stopped working once the surfaces that draw an app stopped painting
 * anything behind its icon. The test is a *filled descendant*: a fill on the root is what the
 * glyph inherits, not what it paints.
 */
function paintsItsOwnColour(root: Element): boolean {
  for (const element of Array.from(root.querySelectorAll("*"))) {
    const fill = element.getAttribute("fill");
    if (fill !== null && collapsed(fill).toLowerCase() !== "none") return true;
  }
  return false;
}

/** Repaint whatever the glyph was taking from the text around it in the tile's ink. */
function inkCurrentColor(root: Element): void {
  for (const element of [root, ...Array.from(root.querySelectorAll("*"))]) {
    for (const attribute of Array.from(element.attributes)) {
      if (collapsed(attribute.value).toLowerCase() === "currentcolor") {
        element.setAttribute(attribute.name, FALLBACK_TILE_INK);
      }
    }
  }
}

/**
 * A glyph that brought no tile, put on the shell's.
 *
 * Drawn into the same centred 32 box a drawn icon's mark sits in, rather than over the whole 48:
 * these glyphs were authored to fill their own frame edge to edge, so at full bleed they crowd
 * the corner radius and read heavier than every icon beside them. A nested `<svg>` does the
 * scaling, so the art keeps its own coordinates and its aspect ratio.
 */
function tiledMarkup(root: Element, sizePx: number): string {
  const document_ = root.ownerDocument;
  const outer = document_.createElementNS(SVG_NAMESPACE, "svg");
  outer.setAttribute("xmlns", SVG_NAMESPACE);
  outer.setAttribute("width", String(sizePx));
  outer.setAttribute("height", String(sizePx));
  outer.setAttribute("viewBox", `0 0 ${TILE_SIZE} ${TILE_SIZE}`);
  outer.setAttribute("fill", "none");
  outer.setAttribute("aria-hidden", "true");
  outer.setAttribute("focusable", "false");

  const tile = document_.createElementNS(SVG_NAMESPACE, "rect");
  tile.setAttribute("width", String(TILE_SIZE));
  tile.setAttribute("height", String(TILE_SIZE));
  tile.setAttribute("rx", String(TILE_RADIUS));
  tile.setAttribute("fill", FALLBACK_TILE_BACKGROUND);
  outer.appendChild(tile);

  inkCurrentColor(root);
  root.setAttribute("x", String(GLYPH_INSET));
  root.setAttribute("y", String(GLYPH_INSET));
  root.setAttribute("width", String(GLYPH_BOX));
  root.setAttribute("height", String(GLYPH_BOX));
  outer.appendChild(root);
  return outer.outerHTML;
}

/**
 * Registry icon markup, made safe to inline at `sizePx`, or null when it is
 * not a usable icon.
 *
 * Null is the answer for anything at all doubtful -- unparseable markup,
 * markup not rooted at a single `<svg>` element, art with no size to scale
 * from, markup over MAX_ICON_LENGTH, or a page with no DOM to sanitize
 * against. Callers draw their own generic glyph on null, so refusing an icon
 * costs a picture rather than a surface.
 */
export function sanitizeIconMarkup(rawMarkup: string, sizePx: number): string | null {
  const markup = rawMarkup.trim();
  if (markup === "" || markup.length > MAX_ICON_LENGTH) return null;
  const key = `${sizePx}|${markup}`;
  const cached = sanitizedByKey.get(key);
  if (cached !== undefined) return cached;
  const sanitized = sanitizeUncached(markup, sizePx);
  if (sanitizedByKey.size >= MAX_CACHE_ENTRIES) sanitizedByKey.clear();
  sanitizedByKey.set(key, sanitized);
  return sanitized;
}

function sanitizeUncached(markup: string, sizePx: number): string | null {
  if (!SVG_OPENING_TAG.test(markup)) return null;
  // No DOM means no parser, and there is no safe way to inline unparsed
  // markup. (This is the server-rendered and unit-test case; the browser
  // always has one.)
  if (!DOMPurify.isSupported) return null;
  const fragment = DOMPurify.sanitize(markup, {
    USE_PROFILES: { svg: true, svgFilters: true },
    FORBID_TAGS: [...FORBIDDEN_TAGS],
    FORBID_ATTR: [...FORBIDDEN_ATTRIBUTES],
    ALLOWED_URI_REGEXP: SCHEMELESS_OR_FRAGMENT_URI,
    RETURN_DOM_FRAGMENT: true,
  });
  const root = onlyElementChild(fragment);
  if (root === null) return null;
  if (root.namespaceURI !== SVG_NAMESPACE || root.tagName.toLowerCase() !== "svg") return null;
  scrubAttributes(root);
  namespaceIds(root, iconIdPrefix(markup));
  if (!normalizeRoot(root, sizePx)) return null;
  return paintsItsOwnColour(root) ? root.outerHTML : tiledMarkup(root, sizePx);
}

/**
 * What to draw for an app: its own icon when it registered a usable one, and
 * the caller's generic glyph otherwise.
 *
 * The fallback is passed in rather than chosen here because each surface's
 * generic glyph is its own, drawn on its own grid.
 */
export function appIconMarkup(
  rawIcon: string | undefined | null,
  sizePx: number,
  fallbackMarkup: string,
  appName?: string,
): string {
  const sanitized = rawIcon === undefined || rawIcon === null ? null : sanitizeIconMarkup(rawIcon, sizePx);
  if (sanitized !== null) return sanitized;
  return appName === undefined ? fallbackMarkup : appMonogramMarkup(appName, sizePx);
}

/**
 * What an app wears when it has registered no icon of its own.
 *
 * Almost every app is in this case, so the fallback cannot be one shared glyph:
 * a list of them all wearing the same box tells the reader nothing. A monogram
 * -- the app's initial on a tile -- at least differs per app and stays put, so
 * the same app is recognisable everywhere it is drawn.
 *
 * It is built to the rules every app icon follows
 * (`docs/system/app-icons.md`): 216 by 216, a flat background under one
 * fill-only mark, a 32 per cent corner. That is what lets it stand beside the
 * drawn icons -- the surfaces that draw an app paint nothing behind its icon,
 * so a transparent monogram would be a thin letter floating on the wallpaper.
 */
export function appMonogramMarkup(appName: string, sizePx: number): string {
  // App names are agent/user text, so the letter is escaped before it lands in
  // markup that callers hand to `m.trust` / `innerHTML`.
  const initial = appName
    .trim()
    .charAt(0)
    .toUpperCase()
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
  return (
    `<svg xmlns="http://www.w3.org/2000/svg" width="${sizePx}" height="${sizePx}" ` +
    `viewBox="0 0 ${TILE_SIZE} ${TILE_SIZE}" ` +
    `fill="none" aria-hidden="true">` +
    `<rect width="${TILE_SIZE}" height="${TILE_SIZE}" rx="${TILE_RADIUS}" fill="${FALLBACK_TILE_BACKGROUND}"/>` +
    `<text x="108" y="114.3" fill="${FALLBACK_TILE_INK}" font-size="108" font-weight="600" ` +
    `text-anchor="middle" dominant-baseline="central">${initial}</text></svg>`
  );
}

/**
 * The same, for surfaces that hold the app's record (or nothing, when the app a window or a
 * shortcut names has since been deregistered): the record's icon, its monogram, or the caller's
 * generic glyph when there is no app to draw.
 *
 * An unknown app has no icon to draw and takes the fallback: there is no app to monogram, and
 * inventing one would dress up a dead name as a real app.
 */
export function appIconMarkupForApp(
  app: Pick<AppRecord, "name" | "icon"> | undefined,
  sizePx: number,
  fallbackMarkup: string,
): string {
  if (app === undefined) return fallbackMarkup;
  return appIconMarkup(app.icon, sizePx, fallbackMarkup, app.name);
}

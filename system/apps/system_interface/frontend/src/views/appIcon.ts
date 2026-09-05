/**
 * An app's own icon, made safe to inline, and the fallback rule for apps
 * without one.
 *
 * An app registers its icon as SVG markup (`forward_port.py --icon`), the
 * registry carries it verbatim on the app's row, and the server hands it to
 * this UI on `AppRecord.icon`. That markup is authored by a skill, so it is
 * untrusted: every surface that draws an app -- the rail's shortcut rows and
 * tab list, the "All apps" popover, the New Tab launcher's tables, the dock
 * tab -- goes through `appIconMarkup`/`serviceIconMarkup` here, and nothing
 * inlines a registry string on its own.
 *
 * The gate is `sanitizeIconMarkup`, and it is deliberately the only one:
 *
 * - Parsing is DOMPurify's, not ours. Hand-rolling HTML validation is how
 *   mutation-XSS gets in; DOMPurify parses the markup with the browser's own
 *   parser and keeps only an SVG allowlist.
 * - Anything that can execute or reach off the page is refused rather than
 *   repaired at the edges: `<script>`, `<a>` (navigates), `<image>` and
 *   `<iframe>` (load), `<foreignObject>` (embeds arbitrary HTML), the
 *   animation elements (`<animate>` and friends, which can retarget an
 *   attribute at runtime), `<style>` and `style=` (CSS can fetch), `on*`
 *   handlers, and any URI that is not a `#fragment` inside the icon itself.
 * - `class` goes too. With no stylesheet of its own an icon's classes can only
 *   match the workspace's, which is a way to borrow layout rules rather than a
 *   way to draw.
 * - The result must be exactly one `<svg>` element. Two roots, a text node
 *   beside the root, or anything that is not an `<svg>` is not an icon, and is
 *   rejected whole -- the caller then draws its built-in glyph.
 *
 * This is defense in depth, not the only defense: `forward_port.py` validates
 * on the way into the registry, and the shell's inventory hands the row's
 * markup through as it is. This is the last check because it is the one that
 * runs against the DOM the markup is about to enter.
 *
 * Sizing and color live here too, since both are decided from the same parsed
 * tree. The icon is rendered at the caller's pixel size on the caller's own
 * grid (its `viewBox` is kept, or derived from its width/height, so the art
 * scales rather than crops), and an icon that names no root `fill` inherits
 * `currentColor` the way the built-in glyphs do -- while one that paints
 * itself keeps every color it asked for.
 */

import DOMPurify from "dompurify";
import { getApp } from "../models/Inventory";

const SVG_NAMESPACE = "http://www.w3.org/2000/svg";

// The same cap `forward_port.py` (MAX_ICON_LENGTH) enforces on the way into
// the registry. Repeated here because this module is the last thing between
// the markup and the DOM, and it must not depend on that check having run.
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
 * they apply to is the only way to be sure of them. It is also what the tests
 * assert against.
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
 * Registry icon markup, made safe to inline at `sizePx`, or null when it is
 * not a usable icon.
 *
 * Null is the answer for anything at all doubtful -- unparseable markup,
 * markup not rooted at a single `<svg>` element, art with no size to scale
 * from,
 * markup over MAX_ICON_LENGTH, or a page with no DOM to sanitize against.
 * Callers draw their own generic glyph on null, so refusing an icon costs a
 * picture rather than a surface.
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
  return root.outerHTML;
}

/**
 * What to draw for an app: its own icon when it registered a usable one, and
 * the caller's generic glyph otherwise.
 *
 * The fallback is passed in rather than chosen here because each surface's
 * generic glyph is its own -- the rail, the launcher and the dock tab each draw
 * an app on their own grid -- and an app without an icon must look exactly as
 * it looked before icons existed.
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
 * a list of them all wearing the same box tells the reader nothing, which is
 * what "notes" and "counter" looked like side by side. A monogram -- the app's
 * initial in an outlined tile -- at least differs per app and stays put, so the
 * same app is recognisable in the rail, the launcher and its tab.
 *
 * It is drawn in the house icon style: currentColor strokes on a transparent
 * background, the same frame every glyph in icons.ts uses, so an unnamed app
 * sits beside the built-in kinds instead of introducing colour the rest of the
 * chrome does not have. (Colour is the projects' identity language -- the
 * squiggles and their palette -- not the apps'.)
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
    `<svg xmlns="http://www.w3.org/2000/svg" width="${sizePx}" height="${sizePx}" viewBox="0 0 24 24" ` +
    `fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" ` +
    `aria-hidden="true"><rect x="3" y="3" width="18" height="18" rx="4"/>` +
    `<text x="12" y="12.7" stroke="none" fill="currentColor" font-size="11" font-weight="600" ` +
    `text-anchor="middle" dominant-baseline="central">${initial}</text></svg>`
  );
}

/**
 * The same, for surfaces that hold an app name rather than the app record: the
 * rail's tab list, the launcher's tables and the dock tab all address an app by
 * the name in its address.
 *
 * An unknown name (an app that has since been deregistered) has no icon to draw
 * and takes the fallback.
 */
export function serviceIconMarkup(appName: string | null, sizePx: number, fallbackMarkup: string): string {
  if (appName === null) return fallbackMarkup;
  const app = getApp(appName);
  if (app === undefined) return fallbackMarkup;
  return appIconMarkup(app.icon, sizePx, fallbackMarkup, app.name);
}

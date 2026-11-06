/**
 * The design system's tokens, read out of the shared library's stylesheet (``base.css``) rather than listed here, so
 * a token added there appears in the catalog with no edit to this app.
 *
 * ``base.css`` keeps every raw value once, as a ``--c-*`` (or scale) custom property on ``:root``, registers each
 * color under its semantic utility name in an ``@theme inline`` block grouped by comment headers, and keeps the
 * font stacks, spacing base, radii and shadows in a plain ``@theme`` block. This module reads those three blocks.
 */

/** One custom property as declared: its name without the leading ``--``, and its value as written. */
export interface DeclaredToken {
  readonly name: string;
  readonly value: string;
}

/** The utility prefix a color is drawn with in the catalog (and most often used with). */
export type ColorRole = "text" | "border" | "bg";

/** One color: ``text-primary`` is ``{ utility: "text-primary", name: "primary", variable: "c-text-primary" }``. */
export interface ColorToken {
  readonly name: string;
  readonly role: ColorRole;
  /** The utility a view writes, or null for a raw value no utility is registered over. */
  readonly utility: string | null;
  /** The ``--c-*`` value the utility resolves through. */
  readonly variable: string;
  /** The value as ``:root`` declares it (a hex, an ``rgb()`` tint, a ``color-mix()``). */
  readonly value: string;
}

/** The colors under one of the ``@theme inline`` block's comment headers ("text", "borders", "status", ...). */
export interface ColorGroup {
  readonly label: string;
  readonly colors: readonly ColorToken[];
}

export interface DesignTokens {
  readonly colorGroups: readonly ColorGroup[];
  /** Every ``:root`` property that is not a color: the type, motion, stacking and width scales, and the shadow
   *  values (``--c-shadow-*``), which sit in the value layer so a theme can redeclare them but draw no swatch. */
  readonly scales: readonly DeclaredToken[];
  /** The plain ``@theme`` block: font stacks, the spacing base, radii and shadows. */
  readonly theme: readonly DeclaredToken[];
}

/** A ``--c-*`` value that is not a color: the elevation values a theme may redeclare. */
function isColorValue(name: string): boolean {
  return name.startsWith("c-") && !name.startsWith("c-shadow-");
}

const COMMENT_OR_DECLARATION = /\/\*([\s\S]*?)\*\/|--([\w-]+)\s*:\s*([^;]+);/g;
const VAR_REFERENCE = /^var\(--([\w-]+)\)$/;

/** The text between the braces of the first block whose header matches ``header``, or "" when there is none. */
export function blockBody(css: string, header: RegExp): string {
  const match = header.exec(css);
  if (match === null) return "";
  const open = css.indexOf("{", match.index + match[0].length - 1);
  let depth = 0;
  for (let index = open; index < css.length; index += 1) {
    if (css[index] === "{") depth += 1;
    if (css[index] === "}") {
      depth -= 1;
      if (depth === 0) return css.slice(open + 1, index);
    }
  }
  return css.slice(open + 1);
}

/** A block's comments and declarations, in source order. */
function entries(body: string): ({ kind: "comment"; text: string } | ({ kind: "token" } & DeclaredToken))[] {
  return Array.from(body.matchAll(COMMENT_OR_DECLARATION), (match) =>
    match[1] !== undefined
      ? { kind: "comment" as const, text: match[1].replace(/\s+/g, " ").trim() }
      : { kind: "token" as const, name: match[2], value: match[3].replace(/\s+/g, " ").trim() },
  );
}

function declarations(body: string): DeclaredToken[] {
  return entries(body).flatMap((entry) => (entry.kind === "token" ? [{ name: entry.name, value: entry.value }] : []));
}

/** A group header's label: the words before its colon ("text: text-primary / ..." reads "text"). */
function groupLabel(comment: string): string {
  const colon = comment.indexOf(":");
  return (colon === -1 ? comment : comment.slice(0, colon)).trim();
}

function roleOf(groupName: string, utility: string): ColorRole {
  if (groupName.startsWith("text") || groupName.startsWith("progress")) return "text";
  if (groupName.startsWith("border") || utility.endsWith("-border")) return "border";
  return "bg";
}

export function readDesignTokens(css: string): DesignTokens {
  const root = declarations(blockBody(css, /:root\s*\{/));
  const valueOf = new Map(root.map((token) => [token.name, token.value]));

  const colorGroups: { label: string; colors: ColorToken[] }[] = [];
  const mapped = new Set<string>();
  for (const entry of entries(blockBody(css, /@theme\s+inline\s*\{/))) {
    if (entry.kind === "comment") {
      colorGroups.push({ label: groupLabel(entry.text), colors: [] });
      continue;
    }
    const reference = VAR_REFERENCE.exec(entry.value);
    if (!entry.name.startsWith("color-") || reference === null) continue;
    if (colorGroups.length === 0) colorGroups.push({ label: "colors", colors: [] });
    const group = colorGroups[colorGroups.length - 1];
    const name = entry.name.slice("color-".length);
    const variable = reference[1];
    mapped.add(variable);
    const role = roleOf(group.label, name);
    group.colors.push({ name, role, utility: `${role}-${name}`, variable, value: valueOf.get(variable) ?? "" });
  }

  // A raw color value with no utility over it still belongs in the catalog, under a group that says so.
  const unmapped = root.filter((token) => isColorValue(token.name) && !mapped.has(token.name));
  if (unmapped.length > 0) {
    colorGroups.push({
      label: "values with no utility",
      colors: unmapped.map((token) => ({
        name: token.name,
        role: "bg" as const,
        utility: null,
        variable: token.name,
        value: token.value,
      })),
    });
  }

  return {
    colorGroups: colorGroups.filter((group) => group.colors.length > 0),
    scales: root.filter((token) => !isColorValue(token.name)),
    theme: declarations(blockBody(css, /@theme\s*\{/)),
  };
}

/** The tokens whose name starts with ``prefix`` (``"radius-"``), in declaration order. */
export function tokensWithPrefix(tokens: readonly DeclaredToken[], prefix: string): DeclaredToken[] {
  return tokens.filter((token) => token.name.startsWith(prefix));
}

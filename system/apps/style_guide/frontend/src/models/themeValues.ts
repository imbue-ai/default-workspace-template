/**
 * What the worn theme sets a token to, read from the theme's own stylesheets (the bundle the theme client links,
 * marked `data-workspace-theme`, and the files it imports). A token the theme leaves alone answers null, so the
 * catalog marks a value as the theme's only when the theme sets it.
 */

import { THEME_LINK_ATTRIBUTE } from "@imbue/workspace-ui/src/themes/themeClient";

/** The `:root` declarations of every stylesheet a theme brought, later files winning. */
function themeRootValues(doc: Document): Map<string, string> {
  const values = new Map<string, string>();
  const visit = (sheet: CSSStyleSheet): void => {
    let rules: CSSRuleList;
    try {
      rules = sheet.cssRules;
    } catch {
      return;
    }
    for (const rule of Array.from(rules)) {
      if (rule instanceof CSSImportRule) {
        if (rule.styleSheet !== null) visit(rule.styleSheet);
      } else if (
        rule instanceof CSSStyleRule &&
        rule.selectorText.split(",").some((part) => part.trim() === ":root")
      ) {
        for (const property of Array.from(rule.style)) {
          if (property.startsWith("--")) values.set(property.slice(2), rule.style.getPropertyValue(property).trim());
        }
      }
    }
  };
  for (const sheet of Array.from(doc.styleSheets)) {
    const owner = sheet.ownerNode;
    if (owner instanceof Element && owner.hasAttribute(THEME_LINK_ATTRIBUTE)) visit(sheet);
  }
  return values;
}

/** The last walk, kept while the same theme stylesheets are on the page: every caption of a redraw asks. */
let cached: { doc: Document; key: string; values: Map<string, string> } | null = null;

function cacheKey(doc: Document): string {
  const links = Array.from(
    doc.querySelectorAll(`[${THEME_LINK_ATTRIBUTE}]`),
    (link) => link.getAttribute("href") ?? "",
  );
  return `${links.join(" ")}|${doc.styleSheets.length}`;
}

/** The worn theme's value for the token `name`, or null when the theme does not set it (or none is worn). */
export function themeValue(name: string, doc: Document = document): string | null {
  const key = cacheKey(doc);
  if (cached === null || cached.doc !== doc || cached.key !== key) cached = { doc, key, values: themeRootValues(doc) };
  return cached.values.get(name) ?? null;
}

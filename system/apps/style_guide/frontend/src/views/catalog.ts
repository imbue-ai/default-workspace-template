/**
 * The catalog's building blocks: a section (a heading, a blurb, and its demo), the groups the contents list
 * gathers them under, and the small labels every demo shares.
 */

import m from "mithril";

/** One catalog entry, jumped to from the contents list. */
export interface CatalogSection {
  /** The element id, which the contents list links to and an element reference names. */
  readonly id: string;
  /** The short label in the contents list. */
  readonly toc: string;
  readonly title: string;
  readonly desc?: m.Children;
  readonly body: () => m.Children;
}

/** A top-level group of the contents list. */
export interface CatalogGroup {
  readonly id: string;
  readonly label: string;
  readonly description: string;
  readonly sections: readonly CatalogSection[];
}

/** An inline token or class name inside a blurb. */
export function code(text: string): m.Vnode {
  return m("code", { class: "font-mono text-(length:--font-size-helper) text-primary" }, text);
}

/** The monospace caption under a demo: a class name, a variable, a value. */
export function caption(text: string, extra = ""): m.Vnode {
  return m("code", { class: `block font-mono type-helper text-secondary select-all ${extra}` }, text);
}

/** A quieter second caption line (a raw value under its name). */
export function subCaption(text: string): m.Vnode {
  return m("code", { class: "block font-mono type-helper text-faint break-all" }, text);
}

/** A row of demos with a label on the left, for a component's variants or states. */
export function demoRow(label: string, children: m.Children): m.Vnode {
  return m("div", { class: "demo-row flex flex-col gap-2 py-3 border-t border-subtle first:border-t-0" }, [
    m("span", { class: "type-section text-faint" }, label),
    m("div", { class: "flex flex-wrap items-center gap-3" }, children),
  ]);
}

/** A section as the catalog draws it. */
export function renderSection(section: CatalogSection): m.Vnode {
  return m("section", { id: section.id, class: "catalog-section mb-12 scroll-mt-8" }, [
    m("h2", { class: `type-heading text-primary ${section.desc === undefined ? "mb-3" : "mb-1"}` }, section.title),
    section.desc === undefined ? null : m("p", { class: "type-body text-secondary mb-4 max-w-[640px]" }, section.desc),
    section.body(),
  ]);
}

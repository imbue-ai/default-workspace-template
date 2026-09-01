import type m from "mithril";

// Mithril already invokes lifecycle hooks it finds in a *component* vnode's
// attrs (initLifecycle runs on vnode.attrs for components and elements alike),
// so forwarding them to the inner element would run every hook twice with the
// same vnode.dom -- e.g. hoverTooltipAttrs would attach two tooltip instances
// and dispose only one.
const MITHRIL_LIFECYCLE_KEYS = new Set([
  "oninit",
  "oncreate",
  "onbeforeupdate",
  "onupdate",
  "onbeforeremove",
  "onremove",
]);

// Split a component's incoming attrs into the keys the component consumes
// and the passthrough HTML attributes (id, aria-*, data-*, event handlers,
// ...). The component names its own keys; everything else lands on the root
// element.
//
// `class`/`className` never pass through: components spread the passthrough
// after their computed class, so a caller's class would silently replace the
// entire recipe. Additive classes go through each component's `extra` attr.
// Mithril lifecycle hooks never pass through either (see above).
export function splitAttrs<T extends m.Attributes>(attrs: T, ownKeys: readonly (keyof T)[]): m.Attributes {
  const passthrough: m.Attributes = {};
  for (const key of Object.keys(attrs)) {
    if (key === "class" || key === "className") continue;
    if (MITHRIL_LIFECYCLE_KEYS.has(key)) continue;
    if (!(ownKeys as readonly string[]).includes(key)) {
      passthrough[key] = (attrs as m.Attributes)[key];
    }
  }
  return passthrough;
}

// The class-only half of the splitAttrs contract, for components that accept a
// whole attrs object to merge onto an element they style (Modal's card/overlay).
// Lifecycle hooks stay: on an element vnode they run once and are a documented
// use (e.g. an autofocus oncreate on a modal card).
export function omitClassAttrs(source: Record<string, unknown>): Record<string, unknown> {
  const rest: Record<string, unknown> = {};
  for (const key of Object.keys(source)) {
    if (key === "class" || key === "className") continue;
    rest[key] = source[key];
  }
  return rest;
}

/**
 * What every view is built from: a section with its heading, a hint, a figure with its unit, a meter, and the inks
 * the graphs and meters are drawn in.
 *
 * The Tailwind scanner reads utility names from the literals in this file: keep every utility name a contiguous
 * literal.
 */

import m from "mithril";

/** The inks, as roles and ramp steps so a theme and the workspace color reach them: CPU is the accent, memory the
 * primary (the workspace color itself), and the disk the wallpaper ramp's middle step, which the workspace color
 * derives too but neither of the other two is. */
export const INKS = {
  cpu: { stroke: "stroke-accent-surface", fill: "fill-accent-surface", bar: "bg-accent-surface" },
  memory: { stroke: "stroke-primary-surface", fill: "fill-primary-surface", bar: "bg-primary-surface" },
  disk: { bar: "bg-wallpaper-8" },
} as const;

/** The swatch of each row of the disk breakdown: the disk's ink, lighter down the rows, and a neutral for the
 * rest of the machine's disk. */
export const DISK_SHADES: Record<string, string> = {
  workspace: "bg-wallpaper-8",
  uploads: "bg-wallpaper-7",
  apps: "bg-wallpaper-6",
  caches: "bg-wallpaper-5",
  home: "bg-wallpaper-4",
  other: "bg-neutral-6",
};
export const DISK_SHADE_FALLBACK = "bg-neutral-6";

export const TAG_CLASS = "tag ml-1.5 type-helper normal-case tracking-normal text-body-content-tertiary";
export const ROW_CLASS = "flex justify-between gap-3 type-helper text-body-content-secondary tabular-nums";
export const HINT_CLASS = "type-helper text-body-content-tertiary";

export function section(attrs: m.Attributes, children: m.Children): m.Vnode {
  return m("section", { class: "border-t border-default py-4 first:border-t-0", ...attrs }, children);
}

/** A section's heading, with an optional quieter tag after it ("last 24 hours"). */
export function sectionHeading(text: string, tag: string | null = null): m.Vnode {
  return m("h2", { class: "mb-3 type-section text-body-content-tertiary" }, [
    text,
    tag === null ? null : m("span", { class: TAG_CLASS }, tag),
  ]);
}

export function hint(text: string, attrs: m.Attributes = {}): m.Vnode {
  return m("p", { class: `-mt-1 mb-2 ${HINT_CLASS}`, ...attrs }, text);
}

/** The view's figure: the number, then what it is of. */
export function bigFigure(value: string, unit: m.Children): m.Vnode {
  return m("div", { class: "big-figure type-heading-lg tabular-nums text-body-content" }, [
    m("span", { "data-figure": "" }, value),
    " ",
    m("small", { class: "type-body text-body-content-secondary" }, unit),
  ]);
}

/** A meter filled to ``percent`` in ``ink``; ``children`` sit over it (the memory guard's mark). */
export function meter(percent: number, ink: string, extra: string, children: m.Children = null): m.Vnode {
  return m("div", { class: `meter flex rounded-sm bg-fill-active ${extra}` }, [
    m("span", {
      class: `block h-full rounded-sm transition-[width] duration-(--dur-base) ${ink}`,
      style: { width: `${percent}%` },
      "data-meter-fill": "",
    }),
    children,
  ]);
}

/** A status dot: live (success), warn, bad (danger), or none. */
export function dot(tone: "none" | "live" | "warn" | "bad", extra = ""): m.Vnode {
  const color = {
    none: "bg-body-content-tertiary",
    live: "bg-success-surface",
    warn: "bg-warn-surface",
    bad: "bg-danger-surface",
  }[tone];
  return m("span", {
    class: `inline-block size-[7px] shrink-0 rounded-full ${color} ${extra}`,
    "aria-hidden": "true",
  });
}

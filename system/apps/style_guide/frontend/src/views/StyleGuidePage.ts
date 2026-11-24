/**
 * The style guide: a sticky contents list beside the catalog's groups. The page scrolls inside its own container
 * (the shared base stylesheet pins the body to the viewport); the contents entry for the section the reader is in is
 * highlighted, and the column scrolls to keep it in view.
 */

import m from "mithril";

import type { DesignTokens } from "../models/tokens";
import { renderSection, type CatalogGroup } from "./catalog";
import { ComponentDemos, componentsGroup } from "./components";
import { foundationsGroup } from "./foundations";
import { themingGroup, wornThemeId } from "./theming";

export interface StyleGuidePageAttrs {
  readonly tokens: DesignTokens;
}

const SCROLL_ID = "catalog-scroll";

function scrollTo(id: string): void {
  document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
}

function tocLink(id: string, label: string, className: string, isActive: boolean): m.Vnode {
  return m(
    "a",
    {
      href: `#${id}`,
      "data-toc-target": id,
      "aria-current": isActive ? "location" : undefined,
      class: `toc-link block rounded-md px-2 hover:bg-fill-hover hover:text-primary ${className} ${
        isActive ? "bg-fill-hover text-primary" : ""
      }`,
      onclick: (event: MouseEvent) => {
        event.preventDefault();
        scrollTo(id);
      },
    },
    label,
  );
}

/** How far below the top of the scroll area a section's top must have passed for it to be the current one. */
const CURRENT_LINE_OFFSET = 96;
/** How close to the left column's edges the current entry may come before the column scrolls to keep it in view. */
const NAV_KEEP_IN_VIEW_MARGIN = 24;

/** The section the reader is in: the last whose top has passed the current line, or the last of all at the end. */
export function currentSectionId(scroller: HTMLElement, targets: readonly Element[]): string | null {
  if (targets.length === 0) return null;
  if (scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 2) return targets[targets.length - 1].id;
  const line = scroller.getBoundingClientRect().top + CURRENT_LINE_OFFSET;
  let current = targets[0].id;
  for (const target of targets) {
    if (target.getBoundingClientRect().top <= line) current = target.id;
  }
  return current;
}

/** Scroll the left column, and only it, just enough to show its current entry. (``scrollIntoView`` would also
 *  scroll the page the column sits in.) */
export function keepCurrentEntryInView(nav: HTMLElement): void {
  const entry = nav.querySelector("[aria-current]");
  if (entry === null) return;
  const box = nav.getBoundingClientRect();
  const entryBox = entry.getBoundingClientRect();
  if (entryBox.top < box.top + NAV_KEEP_IN_VIEW_MARGIN) {
    nav.scrollTop -= box.top + NAV_KEEP_IN_VIEW_MARGIN - entryBox.top;
  } else if (entryBox.bottom > box.bottom - NAV_KEEP_IN_VIEW_MARGIN) {
    nav.scrollTop += entryBox.bottom - (box.bottom - NAV_KEEP_IN_VIEW_MARGIN);
  }
}

export function StyleGuidePage(): m.Component<StyleGuidePageAttrs> {
  const demos = new ComponentDemos();
  let activeId: string | null = null;
  let stopTracking: (() => void) | null = null;

  return {
    oncreate({ dom }) {
      const scroller = dom.querySelector<HTMLElement>(`#${SCROLL_ID}`);
      const nav = dom.querySelector<HTMLElement>("nav.toc");
      if (scroller === null || nav === null) return;
      const targets = Array.from(dom.querySelectorAll(".catalog-section, .catalog-group"));
      let frame: number | null = null;
      const update = (): void => {
        frame = null;
        const next = currentSectionId(scroller, targets);
        if (next === activeId) return;
        activeId = next;
        m.redraw.sync();
        keepCurrentEntryInView(nav);
      };
      const onScroll = (): void => {
        if (frame === null) frame = requestAnimationFrame(update);
      };
      scroller.addEventListener("scroll", onScroll, { passive: true });
      window.addEventListener("resize", onScroll);
      stopTracking = () => {
        scroller.removeEventListener("scroll", onScroll);
        window.removeEventListener("resize", onScroll);
        if (frame !== null) cancelAnimationFrame(frame);
      };
      update();
    },
    onremove() {
      stopTracking?.();
      demos.dispose();
    },
    view({ attrs: { tokens } }) {
      const catalog: readonly CatalogGroup[] = [foundationsGroup(tokens), componentsGroup(demos)];
      const labels = new Map(catalog.flatMap((group) => group.sections.map((section) => [section.id, section.toc])));
      // The page kit is the Theming group's own section, so it is named here rather than looked up.
      labels.set("page-kit", "Plain pages");
      const groups: readonly CatalogGroup[] = [...catalog, themingGroup((id) => labels.get(id) ?? id)];
      return m("div", { class: "style-guide flex h-screen bg-surface" }, [
        m(
          "nav",
          {
            "aria-label": "Style guide sections",
            // A full-height column of its own, scrolling on its own in a short window: a thin bar, in a gutter of its
            // own with room before it, so a system that always shows scrollbars never lays one over the labels.
            class:
              "toc hidden h-full w-60 shrink-0 overflow-y-auto py-10 pr-3 pl-8 " +
              "[scrollbar-gutter:stable] [scrollbar-width:thin] md:block",
          },
          groups.map((group) =>
            m("div", { class: "mb-5" }, [
              tocLink(group.id, group.label, "type-section py-1 text-faint", activeId === group.id),
              m(
                "ul",
                { class: "mt-1 flex flex-col gap-px" },
                group.sections.map((section) =>
                  m(
                    "li",
                    tocLink(
                      section.id,
                      section.toc,
                      "py-1 text-(length:--font-size-row) text-secondary",
                      activeId === section.id,
                    ),
                  ),
                ),
              ),
            ]),
          ),
        ),
        m(
          "div",
          { id: SCROLL_ID, class: "h-full min-w-0 flex-1 overflow-y-auto" },
          m("main", { class: "mx-auto max-w-5xl px-8 py-10" }, [
            m("header", { class: "mb-10" }, [
              m("h1", { class: "type-heading-lg text-primary" }, "Workspace Style Guide"),
              m("p", { class: "type-body mt-1 max-w-[640px] text-secondary" }, [
                "The design system every workspace app draws with. Tokens live in ",
                m("code", { class: "font-mono text-primary" }, "system/libs/workspace_ui/src/base.css"),
                ", components in ",
                m("code", { class: "font-mono text-primary" }, "system/libs/workspace_ui/src/components/"),
                ". A convention for the default look, not a rule: a restyle to your own taste is always welcome.",
              ]),
              m("p", { id: "worn-theme", class: "type-helper mt-2 text-secondary" }, [
                "Showing the ",
                m("code", { class: "font-mono text-primary" }, wornThemeId()),
                " theme, the one this desktop wears; values a theme changed show the standard one struck through.",
              ]),
            ]),
            groups.map((group, index) => [
              m(
                "div",
                {
                  id: group.id,
                  class: `catalog-group mb-8 scroll-mt-8 ${index === 0 ? "" : "mt-16 border-t border-default pt-12"}`,
                },
                [
                  m("h2", { class: "type-heading-lg text-primary" }, group.label),
                  m("p", { class: "type-body mt-1 text-secondary" }, group.description),
                ],
              ),
              group.sections.map(renderSection),
            ]),
          ]),
        ),
      ]);
    },
  };
}

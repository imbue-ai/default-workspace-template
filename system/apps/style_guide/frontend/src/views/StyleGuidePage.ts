/**
 * The style guide: a sticky contents list beside the catalog, which is split into Foundations (the tokens and the
 * icon set), Components (the shared controls), and Theming (the parts a theme styles). The page scrolls inside its own container (the shared base
 * stylesheet pins the body to the viewport), and the contents entry for the section nearest the top of it reads at
 * full strength.
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

export function StyleGuidePage(): m.Component<StyleGuidePageAttrs> {
  const demos = new ComponentDemos();
  let activeId: string | null = null;
  let observer: IntersectionObserver | null = null;
  const visible = new Map<string, boolean>();

  function recomputeActive(targets: readonly Element[]): void {
    let next: string | null = null;
    let nearestTop = Infinity;
    for (const target of targets) {
      if (visible.get(target.id) !== true) continue;
      const top = target.getBoundingClientRect().top;
      if (top < nearestTop) {
        nearestTop = top;
        next = target.id;
      }
    }
    if (next !== null && next !== activeId) {
      activeId = next;
      m.redraw();
    }
  }

  return {
    oncreate({ dom }) {
      if (!("IntersectionObserver" in window)) return;
      const targets = Array.from(dom.querySelectorAll(".catalog-section, .catalog-group"));
      observer = new IntersectionObserver(
        (entries) => {
          for (const entry of entries) visible.set(entry.target.id, entry.isIntersecting);
          recomputeActive(targets);
        },
        // A band near the top of the scroll area: a section is current while it crosses it.
        { root: dom.querySelector(`#${SCROLL_ID}`), rootMargin: "-5% 0px -80% 0px", threshold: 0 },
      );
      for (const target of targets) observer.observe(target);
    },
    onremove() {
      observer?.disconnect();
      demos.dispose();
    },
    view({ attrs: { tokens } }) {
      const catalog: readonly CatalogGroup[] = [foundationsGroup(tokens), componentsGroup(demos)];
      const labels = new Map(catalog.flatMap((group) => group.sections.map((section) => [section.id, section.toc])));
      // The page kit is the Theming group's own section, so it is named here rather than looked up.
      labels.set("page-kit", "Plain pages");
      const groups: readonly CatalogGroup[] = [...catalog, themingGroup((id) => labels.get(id) ?? id)];
      return m("div", { class: "style-guide h-screen bg-surface" }, [
        m(
          "div",
          { id: SCROLL_ID, class: "h-full overflow-y-auto" },
          m("div", { class: "mx-auto flex max-w-6xl items-start gap-10 px-8 py-10" }, [
            m(
              "nav",
              {
                "aria-label": "Style guide sections",
                class: "toc sticky top-10 hidden max-h-[calc(100vh-5rem)] w-44 shrink-0 overflow-y-auto md:block",
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
            m("main", { class: "min-w-0 flex-1" }, [
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
          ]),
        ),
      ]);
    },
  };
}

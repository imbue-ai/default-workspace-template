/**
 * The "Start from a template" cards and rails of the New Tab page: a card is a template's drawing
 * in a 3:2 frame with its title and byline under it; a shelf is a heading over a sideways rail of
 * cards that shows three and a half at a time, pages one visible width with the arrows overlaying
 * its ends, and scrolls freely with the trackpad. Picking a card is the launcher's business (it
 * opens the detail dialog), so both components only report the pick.
 *
 * The rail arithmetic (which arrows to show, where a page lands) is exported as pure functions so
 * it can be tested without a DOM.
 */

import m from "mithril";
import type { CatalogTemplate, ResolvedShelf } from "../models/TemplateCatalog";
import { buttonClass } from "@imbue/workspace-ui/src/components/Button";
import { icon } from "@imbue/workspace-ui/src/components/icons";

const CARD_FALLBACK_GLYPH_SIZE = 20;
const RAIL_ARROW_GLYPH_SIZE = 16;

// A card is sized so the rail shows exactly three and a half: with three 24px gaps before the
// half one, 3.5w + 3*24px is the rail's width. The sliced card is what says the rail scrolls.
const CARD_WIDTH_CLASS = "w-[calc((100%-72px)/3.5)]";

/** What a rail measures about itself, read off the scroll container. */
export interface RailExtent {
  scrollLeft: number;
  clientWidth: number;
  scrollWidth: number;
}

/** Whether a rail can page left (it has scrolled) or right (there is more past its edge). */
export function railPaging(extent: RailExtent): { canPageLeft: boolean; canPageRight: boolean } {
  return {
    canPageLeft: extent.scrollLeft > 1,
    canPageRight: extent.scrollLeft + extent.clientWidth < extent.scrollWidth - 1,
  };
}

/** Where one page of the rail lands: a visible width along, clamped to the rail's ends. */
export function railPageTarget(extent: RailExtent, direction: -1 | 1): number {
  const farthest = Math.max(0, extent.scrollWidth - extent.clientWidth);
  return Math.min(farthest, Math.max(0, extent.scrollLeft + direction * extent.clientWidth));
}

export interface TemplateCardAttrs {
  template: CatalogTemplate;
  // Whether the card fills its grid cell (a search result) rather than taking a rail's card width.
  isFill: boolean;
  onPick: (template: CatalogTemplate) => void;
}

/** One template as a card: its drawing (or a glyph when there is none or it failed to load), its title, its byline. */
export function TemplateCard(): m.Component<TemplateCardAttrs> {
  // Set once the drawing fails to load; the card shows the generic glyph from then on.
  let isArtBroken = false;

  return {
    view(vnode) {
      const { template, isFill, onPick } = vnode.attrs;
      const hasArt = template.thumbnail_url !== "" && !isArtBroken;
      return m(
        "button",
        {
          type: "button",
          "data-template": template.slug,
          class:
            "new-tab-template-card group shrink-0 snap-start cursor-pointer text-left " +
            (isFill ? "w-full" : CARD_WIDTH_CLASS),
          onclick: () => onPick(template),
        },
        [
          // The drawings are all 3:2, so the frame matches and nothing is cropped; a missing one
          // gets a quiet glyph on the page tint rather than a hole in the rail.
          m(
            "span",
            {
              class:
                "block aspect-[3/2] overflow-hidden rounded-lg bg-page transition-[transform,box-shadow] " +
                "duration-(--dur-slow) group-hover:scale-[1.02] group-hover:shadow-overlay",
            },
            hasArt
              ? m("img", {
                  src: template.thumbnail_url,
                  alt: "",
                  loading: "lazy",
                  class: "h-full w-full object-cover",
                  onerror: () => {
                    isArtBroken = true;
                  },
                })
              : m(
                  "span",
                  { class: "flex h-full w-full items-center justify-center text-faint" },
                  m.trust(icon("box", { size: CARD_FALLBACK_GLYPH_SIZE })),
                ),
          ),
          m("span", { class: "mt-2 block truncate text-(length:--font-size-body) text-primary" }, template.title),
          template.author === ""
            ? null
            : m("span", { class: "type-helper block truncate text-faint" }, `by ${template.author}`),
        ],
      );
    },
  };
}

export interface TemplateShelvesAttrs {
  shelves: readonly ResolvedShelf[];
  onPick: (template: CatalogTemplate) => void;
}

/** The catalog's rows as rails of cards, each with the paging arrows its scroll position calls for. */
export function TemplateShelves(): m.Component<TemplateShelvesAttrs> {
  // Each rail's last measured extent, by shelf key: what decides which paging arrows it shows.
  const railExtentByShelf = new Map<string, RailExtent>();

  function measured(rail: HTMLElement): RailExtent {
    return { scrollLeft: rail.scrollLeft, clientWidth: rail.clientWidth, scrollWidth: rail.scrollWidth };
  }

  function measureRail(shelfKey: string, rail: HTMLElement): void {
    const extent = measured(rail);
    const previous = railExtentByShelf.get(shelfKey);
    if (
      previous !== undefined &&
      previous.scrollLeft === extent.scrollLeft &&
      previous.clientWidth === extent.clientWidth &&
      previous.scrollWidth === extent.scrollWidth
    ) {
      return;
    }
    railExtentByShelf.set(shelfKey, extent);
    // Measured outside an event handler (on mount, or after a layout change): redraw so the
    // arrows follow. A repeat measurement is equal and returns above, so this cannot loop.
    m.redraw();
  }

  function scrollRailTo(rail: HTMLElement, left: number): void {
    if (typeof rail.scrollTo === "function") {
      rail.scrollTo({ left, behavior: "smooth" });
    } else {
      rail.scrollLeft = left;
    }
  }

  function railArrow(shelf: ResolvedShelf, direction: -1 | 1): m.Vnode {
    const isRight = direction === 1;
    return m(
      "button",
      {
        type: "button",
        "aria-label": isRight ? "Show more templates" : "Show previous templates",
        "data-rail-page": isRight ? "next" : "previous",
        class: buttonClass("secondary", {
          icon: true,
          sm: true,
          round: true,
          extra:
            "new-tab-template-rail-arrow absolute top-1/2 z-(--z-content) -translate-y-1/2 shadow-overlay " +
            (isRight ? "right-1" : "left-1"),
        }),
        onclick: (event: MouseEvent) => {
          const rail = (event.currentTarget as HTMLElement).parentElement?.querySelector<HTMLElement>(
            ".new-tab-template-rail",
          );
          if (!rail) return;
          scrollRailTo(rail, railPageTarget(railExtentByShelf.get(shelf.key) ?? measured(rail), direction));
        },
      },
      m.trust(icon(isRight ? "chevron-right" : "chevron-left", { size: RAIL_ARROW_GLYPH_SIZE })),
    );
  }

  function shelfView(shelf: ResolvedShelf, onPick: (template: CatalogTemplate) => void): m.Vnode {
    const paging = railPaging(railExtentByShelf.get(shelf.key) ?? { scrollLeft: 0, clientWidth: 0, scrollWidth: 0 });
    return m("section", { key: shelf.key, class: "new-tab-template-shelf mt-4 first:mt-0", "data-shelf": shelf.key }, [
      m("h3", { class: "type-label px-2 text-primary" }, shelf.title),
      // The scroller takes the column's padding as its own so a hovered card's lift has room
      // inside the scroll box, and the arrows overlay its ends.
      m("div", { class: "relative mt-2" }, [
        paging.canPageLeft ? railArrow(shelf, -1) : null,
        m(
          "div",
          {
            class: "new-tab-template-rail snap-x overflow-x-auto scroll-pl-2 px-2 pt-1 pb-3",
            oncreate: (vnode: m.VnodeDOM) => measureRail(shelf.key, vnode.dom as HTMLElement),
            onupdate: (vnode: m.VnodeDOM) => measureRail(shelf.key, vnode.dom as HTMLElement),
            onscroll: (event: Event) => measureRail(shelf.key, event.currentTarget as HTMLElement),
          },
          m(
            "div",
            { class: "flex items-start gap-6" },
            shelf.templates.map((template) =>
              m(TemplateCard, { key: template.slug, template, isFill: false, onPick }),
            ),
          ),
        ),
        paging.canPageRight ? railArrow(shelf, 1) : null,
      ]),
    ]);
  }

  return {
    view(vnode) {
      const { shelves, onPick } = vnode.attrs;
      return m(
        "div",
        { class: "mt-3" },
        shelves.map((shelf) => shelfView(shelf, onPick)),
      );
    },
  };
}

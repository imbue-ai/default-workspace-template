/**
 * The "Start from a template" cards and rails of the New Tab page: a card is a template's drawing
 * in a 3:2 frame with its title, a two-line description and its byline under it; a shelf is a
 * heading over a sideways rail of cards that shows two and a half at a time, pages one visible
 * width with the arrows overlaying its ends (each a full-height strip that fades the rail out
 * under it), and scrolls freely with the trackpad. Picking a card is the launcher's business (it
 * opens the detail dialog), so both components only report the pick.
 *
 * The type ramp is the point of the section, and it runs shelf heading -> card title ->
 * description -> byline: ``type-heading`` (18px semibold) over ``type-label`` (14px semibold) over
 * ``type-helper`` on secondary over ``type-helper`` on faint. Only the heading grows -- one step,
 * to the role above the one it had. Below it the ramp is carried by weight, colour and the space
 * around each line rather than by more size, because the section already starts below the fold on
 * a 13-inch laptop and every pixel of type spent here pushes the first card further down.
 *
 * The rail arithmetic (which arrows to show, where a page lands) is exported as pure functions so
 * it can be tested without a DOM.
 */

import m from "mithril";
import type { CatalogTemplate, ResolvedShelf } from "../models/TemplateCatalog";
import { TemplateArt } from "./TemplateArt";
import { icon } from "@imbue/workspace-ui/src/components/icons";

const CARD_FALLBACK_GLYPH_SIZE = 20;
const RAIL_ARROW_GLYPH_SIZE = 16;

// A card is sized so the rail shows exactly two and a half: with two 24px gaps before the half
// one, 2.5w + 2*24px is the rail's width. The sliced card is what says the rail scrolls.
//
// Two and a half rather than the three and a half this rail used to show. The drawings are
// screenshots of real app interfaces, and the rail lives in the page's 896px column whatever the
// window is, so at three and a half a card measured 231px and its drawing 231x154 -- too small
// on a 13-inch laptop to read as anything but a coloured box, which is the whole of the
// complaint this section answers. At two and a half the card measures 333px and the drawing
// 333x222, and the title gets about 48 characters, which is every title the catalog carries.
const CARD_WIDTH_CLASS = "w-[calc((100%-48px)/2.5)]";

// The description reserves both its lines whether or not it fills them, so every card in a rail
// puts its byline on the same baseline no matter how long -- or how empty -- its description is.
const CARD_DESCRIPTION_CLASS = "type-helper mt-0.5 line-clamp-2 min-h-[2lh] text-secondary";

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

/** One template as a card: its drawing (or a glyph when there is none or it failed to load), its
 *  title, the catalog's one-line description clamped to two lines, its byline. */
export function TemplateCard(): m.Component<TemplateCardAttrs> {
  return {
    view(vnode) {
      const { template, isFill, onPick } = vnode.attrs;
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
          m(TemplateArt, {
            template,
            frameClass:
              "rounded-lg transition-[transform,box-shadow] duration-(--dur-slow) group-hover:scale-[1.02] " +
              "group-hover:shadow-overlay",
            glyphSize: CARD_FALLBACK_GLYPH_SIZE,
          }),
          m("span", { class: "new-tab-template-title type-label mt-2 block truncate text-primary" }, template.title),
          m("span", { class: `new-tab-template-description ${CARD_DESCRIPTION_CLASS}` }, template.description),
          template.author === ""
            ? null
            : m("span", { class: "type-helper mt-0.5 block truncate text-faint" }, `by ${template.author}`),
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

  /**
   * A paging arrow: a strip the full height of the rail at one end, fading from the page surface
   * at the edge to nothing over the cards, with the chevron at its outer side. The strip is the
   * whole target, and it is a plain button rather than the shared recipe: that recipe fixes a
   * button's size, border, fill and radius, none of which a full-height gradient strip can carry.
   */
  function railArrow(shelf: ResolvedShelf, direction: -1 | 1): m.Vnode {
    const isRight = direction === 1;
    return m(
      "button",
      {
        type: "button",
        class:
          "new-tab-template-rail-arrow absolute inset-y-0 z-(--z-content) flex w-16 cursor-pointer items-center " +
          "from-surface to-transparent text-secondary hover:text-primary focus-visible:outline-2 " +
          "focus-visible:-outline-offset-2 focus-visible:outline-accent " +
          (isRight ? "right-0 justify-end bg-linear-to-l pr-2" : "left-0 justify-start bg-linear-to-r pl-2"),
        "aria-label": isRight ? "Show more templates" : "Show previous templates",
        "data-rail-page": isRight ? "next" : "previous",
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
    // A row stands off from the one above it by more than its heading stands off from its own
    // cards: that gap is what makes a row read as its own tray rather than as more of the wall.
    return m("section", { key: shelf.key, class: "new-tab-template-shelf mt-6 first:mt-0", "data-shelf": shelf.key }, [
      m("h3", { class: "new-tab-template-shelf-title type-heading px-2 text-primary" }, shelf.title),
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

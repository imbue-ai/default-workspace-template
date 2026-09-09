/**
 * The "Start from a template" cards and rails of the New Tab page: a card is a template's drawing
 * in a 3:2 frame with its title and byline under it; a shelf is a heading over a sideways rail of
 * cards that shows three and a half at a time, pages one visible width with an arrow in the gutter
 * at each end, and scrolls freely with the trackpad. The arrows sit outside the scrolling area, so
 * the cards run to the rail's edge with nothing over them. Picking a card is the launcher's
 * business (it opens the detail dialog), so both components only report the pick.
 *
 * The rail arithmetic (which arrows to show, where a page lands) is exported as pure functions so
 * it can be tested without a DOM.
 */

import m from "mithril";
import type { CatalogTemplate, ResolvedShelf } from "../models/TemplateCatalog";
import { TemplateArt } from "./TemplateArt";
import { HOVER_LIFT_GROUP } from "./hoverLift";
import { icon } from "@imbue/workspace-ui/src/components/icons";

const CARD_FALLBACK_GLYPH_SIZE = 20;
const RAIL_ARROW_GLYPH_SIZE = 20;

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

/**
 * How far down its sliver a paging arrow's circle sits: level with the middle of the card
 * drawings, which is measured off the laid-out rail, and level with the middle of the sliver
 * itself until that measurement exists (a rail that has not been laid out reports 0).
 */
export function railArrowTop(artCentre: number): string {
  return artCentre > 0 ? `${artCentre}px` : "50%";
}

export interface TemplateCardAttrs {
  template: CatalogTemplate;
  // Whether the card fills its grid cell (a search result) rather than taking a rail's card width.
  isFill: boolean;
  onPick: (template: CatalogTemplate) => void;
}

/** One template as a card: its drawing (or a glyph when there is none or it failed to load), its title, its byline. */
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
            // The whole card is the target, so the title and byline lift the drawing too; the
            // "Start something" tiles wear the same lift.
            frameClass: `${HOVER_LIFT_GROUP} rounded-lg`,
            glyphSize: CARD_FALLBACK_GLYPH_SIZE,
          }),
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
  // Where each rail's card drawings are centred, in pixels down from the row's top: what puts the
  // paging arrows level with the drawings instead of with the titles underneath them. It has to be
  // measured, because a card's width (and so its 3:2 drawing's height) follows the rail's own.
  const artCentreByShelf = new Map<string, number>();

  function measured(rail: HTMLElement): RailExtent {
    return { scrollLeft: rail.scrollLeft, clientWidth: rail.clientWidth, scrollWidth: rail.scrollWidth };
  }

  /** The drawings' centre, down from the top of the row the arrows share with the rail; 0 until
   *  there is a laid-out drawing to measure. */
  function measuredArtCentre(rail: HTMLElement): number {
    const row = rail.parentElement;
    const art = rail.querySelector<HTMLElement>(".new-tab-template-art");
    if (row === null || art === null) return 0;
    const box = art.getBoundingClientRect();
    return box.height === 0 ? 0 : Math.round(box.top + box.height / 2 - row.getBoundingClientRect().top);
  }

  function measureRail(shelfKey: string, rail: HTMLElement): void {
    const extent = measured(rail);
    const artCentre = measuredArtCentre(rail);
    const previous = railExtentByShelf.get(shelfKey);
    if (
      previous !== undefined &&
      previous.scrollLeft === extent.scrollLeft &&
      previous.clientWidth === extent.clientWidth &&
      previous.scrollWidth === extent.scrollWidth &&
      artCentreByShelf.get(shelfKey) === artCentre
    ) {
      return;
    }
    railExtentByShelf.set(shelfKey, extent);
    artCentreByShelf.set(shelfKey, artCentre);
    // Measured outside an event handler (on mount, or after a layout change): redraw so the
    // arrows follow. A repeat measurement is equal and returns above, so this cannot loop --
    // every value compared is a number, so an unmeasurable rail settles on 0 rather than
    // alternating with "not measured yet".
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
   * A paging arrow. The TARGET is the whole sliver beside the rail, the full height of the row, so
   * paging costs no aim; what the pointer LIGHTS UP is only the circle inside it, level with the
   * card drawings. A big invisible target with a small visible control is why this is a plain
   * button and not the shared recipe: the recipe fixes a button's size, fill and radius, and here
   * those belong to the circle rather than to the thing being clicked.
   *
   * The circle is pinned to the sliver's outer edge, which is as far out as the page goes, and
   * hangs a few pixels past the sliver's inner one. What it hangs over is the scroller's padding,
   * which is empty while the rail is at rest -- the cards stop 8px clear of it -- and that padding
   * is also what keeps a hovered card at either end from being clipped (see the row below). A card
   * dragged freely with the trackpad can pass under that overhang; a paged one cannot, since the
   * scroll padding lands it at the same place the cards rest.
   *
   * The focus ring goes on the circle too -- the sliver is tall enough that ringing it would read
   * as a frame around the whole rail.
   */
  function railArrow(shelf: ResolvedShelf, direction: -1 | 1, artCentre: number, canPage: boolean): m.Vnode {
    const isRight = direction === 1;
    const edge = isRight ? "right-0" : "left-0";
    return m(
      "button",
      {
        type: "button",
        // Always here, so an arrow that runs out of rail fades away instead of vanishing between
        // frames; ``disabled`` is what actually takes it out of reach while it is invisible, which
        // covers the pointer, the tab order and assistive tech in one go.
        class:
          `new-tab-template-rail-arrow group absolute inset-y-0 ${edge} w-full ` +
          "transition-opacity duration-(--dur-slow) ease-[ease] focus-visible:outline-none " +
          (canPage ? "cursor-pointer opacity-100" : "opacity-0"),
        disabled: !canPage,
        "aria-label": isRight ? "Show more templates" : "Show previous templates",
        "data-rail-page": isRight ? "next" : "previous",
        onclick: (event: MouseEvent) => {
          const row = (event.currentTarget as HTMLElement).closest(".new-tab-template-rail-row");
          const rail = row?.querySelector<HTMLElement>(".new-tab-template-rail");
          if (!rail) return;
          scrollRailTo(rail, railPageTarget(railExtentByShelf.get(shelf.key) ?? measured(rail), direction));
        },
      },
      m(
        "span",
        {
          class:
            `new-tab-template-rail-arrow-circle absolute ${edge} flex h-6 w-6 -translate-y-1/2 items-center ` +
            "justify-center rounded-full text-secondary transition-[background-color,color] " +
            "duration-(--dur-base) ease-[ease] group-hover:bg-fill-hover group-hover:text-primary " +
            "group-active:bg-fill-active group-focus-visible:outline-2 group-focus-visible:outline-offset-2 " +
            "group-focus-visible:outline-accent",
          style: { top: railArrowTop(artCentre) },
        },
        m.trust(icon(isRight ? "chevron-right" : "chevron-left", { size: RAIL_ARROW_GLYPH_SIZE })),
      ),
    );
  }

  /**
   * One arrow's sliver. It is always in the layout, and so is the arrow inside it, so a rail
   * reaching its end neither resizes the cards under the pointer nor blinks the arrow out.
   */
  function railGutter(shelf: ResolvedShelf, direction: -1 | 1, canPage: boolean): m.Vnode {
    return m(
      "div",
      { class: "relative w-5 shrink-0" },
      railArrow(shelf, direction, artCentreByShelf.get(shelf.key) ?? 0, canPage),
    );
  }

  /**
   * The soft edge on an end that has more rail past it: an overlay in the page's own colour, laid
   * over the scroller's edge and faded out inwards. It is an element rather than a mask on the
   * scroller so that showing and hiding it is opacity, and it takes no pointer events, so the rail
   * still scrolls and the cards still take clicks underneath it. Its ramp is in style.css, where
   * the reasoning about it lives; the width is the same 30px at both ends.
   */
  function railFade(isEnd: boolean, isShown: boolean): m.Vnode {
    return m("div", {
      class:
        `new-tab-template-rail-fade-${isEnd ? "end" : "start"} pointer-events-none absolute inset-y-0 ` +
        `z-(--z-content) w-[30px] transition-opacity duration-(--dur-slow) ease-[ease] ` +
        // Inset by a sliver's width, so the overlay starts where the scroller does and never
        // reaches over an arrow.
        (isEnd ? "right-5" : "left-5"),
      // Written as a style rather than an opacity-* utility on purpose: the utilities did not take
      // on this element (the class landed but the computed opacity stayed 0), and an inline style
      // cannot be out-ordered by anything. The transition above still animates it.
      style: { opacity: isShown ? "1" : "0" },
      "aria-hidden": "true",
      "data-rail-fade": isEnd ? "end" : "start",
    });
  }

  function shelfView(shelf: ResolvedShelf, onPick: (template: CatalogTemplate) => void): m.Vnode {
    const paging = railPaging(railExtentByShelf.get(shelf.key) ?? { scrollLeft: 0, clientWidth: 0, scrollWidth: 0 });
    return m("section", { key: shelf.key, class: "new-tab-template-shelf mt-4 first:mt-0", "data-shelf": shelf.key }, [
      m("h3", { class: "type-label px-2 text-primary" }, shelf.title),
      // The row hangs its slivers out past the page column, so the arrows sit in the page's margin
      // and the cards still line up under the heading: -mx-6 back, w-5 slivers and the scroller's
      // px-3 forward leave the cards at the heading's own px-2. -mx-6 is also the most the page
      // can give, being exactly the launcher's own padding; past that the arrows would fall
      // outside the scroll box and raise a scrollbar.
      //
      // That fixes the budget at 32px, and the scroller's share of it is what keeps a hovered card
      // whole: it clips at its padding edge, so a lifted card at either end of the rail loses
      // whatever grows past that. The 2% growth costs ~2px and the shadow reaches ~6px (a 12px
      // blur), so 8px only just covered it and trimmed the shadow's last of it; 12px clears both
      // with room to spare. The arrows keep their 24px circle by giving up sliver width instead:
      // the circle hangs 4px past its 20px sliver, over the scroller's padding, which no card
      // occupies while the rail is at rest.
      m("div", { class: "new-tab-template-rail-row relative -mx-6 mt-2 flex" }, [
        railGutter(shelf, -1, paging.canPageLeft),
        // The scroller lays the cards out itself rather than wrapping a flex row, because that is
        // what makes its trailing padding real: a scroll container in block layout leaves its
        // end-side padding out of its scrollable extent, so the last card came to rest flush
        // against the edge and had its lift clipped there however much padding was asked for. A
        // flex scroll container keeps it, and both ends clear alike.
        m(
          "div",
          {
            class:
              "new-tab-template-rail flex min-w-0 flex-1 snap-x items-start gap-6 overflow-x-auto " +
              "scroll-pl-3 px-3 pt-1 pb-3",
            oncreate: (vnode: m.VnodeDOM) => measureRail(shelf.key, vnode.dom as HTMLElement),
            onupdate: (vnode: m.VnodeDOM) => measureRail(shelf.key, vnode.dom as HTMLElement),
            onscroll: (event: Event) => measureRail(shelf.key, event.currentTarget as HTMLElement),
          },
          shelf.templates.map((template) => m(TemplateCard, { key: template.slug, template, isFill: false, onPick })),
        ),
        railGutter(shelf, 1, paging.canPageRight),
        // After the scroller, so they lie over the cards rather than under them.
        railFade(false, paging.canPageLeft),
        railFade(true, paging.canPageRight),
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

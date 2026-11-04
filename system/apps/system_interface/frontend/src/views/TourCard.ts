/**
 * The show-around tour's card, the badge the tour leaves when exited early, and the pause notice it shows when the
 * agent's reply interrupted it (the new-onboarding spec, section 2.6). One fixed box over the desktop: the avatar
 * and the card's words, its bullets, a picture or a link when the card has one, and a footer with Exit walkthrough,
 * Go back, and Next (Done on the last card). Where it sits comes from ``model/tourPlacement``: beside the shell's
 * own target when the card has one, popped out of the avatar otherwise, and under the Imbue Studio chrome for a
 * card about one of its controls. Targets move (a window opens, the avatar is dragged), so the card measures again
 * on every redraw, on resize, and on a short interval.
 * Markers the tests use: ``data-tour-card``, ``data-tour-badge``, ``data-tour-paused``, ``data-tour-exit``,
 * ``data-tour-back``, ``data-tour-next``, ``data-tour-link``.
 */

import m from "mithril";
import { Button, buttonClass } from "@imbue/workspace-ui/src/components/Button";
import { avatarImageUrl } from "../model/api";
import type { TourController } from "../model/tour";
import type { ChromeAnchor, TourCard as TourCardRecord } from "../model/tourCards";
import {
  TOUR_EXIT_BADGE_BUTTON_LABEL,
  TOUR_EXIT_BADGE_TEXT,
  TOUR_PAUSED_BUTTON_LABEL,
  TOUR_PAUSED_TEXT,
  tourTargetSelectors,
} from "../model/tourCards";
import type { Box, Size, TourPlacement } from "../model/tourPlacement";
import { placeBeside, placeByAvatar, placeUnderChrome } from "../model/tourPlacement";

const CARD_WIDTH_PX = 360;
const IMAGE_CARD_WIDTH_PX = 440;
/** The arrow is a rotated square of this size, sunk into the card's edge by half of it plus the border. */
const ARROW_SIZE_PX = 12;
const ARROW_INSET = `-${ARROW_SIZE_PX / 2 + 1}px`;
/** How often the card re-measures what it points at while shown. */
const FOLLOW_INTERVAL_MS = 400;

export interface TourCardAttrs {
  readonly tour: TourController;
  /** The workspace's avatar design, drawn beside the card's words. */
  readonly avatarDesign: string;
  /** Opens the card's link where a link of the shell's own chrome opens (the store's link routing). */
  readonly openLink: (url: string) => void;
  /** Where the shell's markers are looked up; the document by default. */
  readonly root?: ParentNode;
  readonly viewport?: Size;
}

function boxOf(element: Element | null): Box | null {
  if (element === null) return null;
  const rect = element.getBoundingClientRect();
  if (rect.width === 0 && rect.height === 0) return null;
  return { left: rect.left, top: rect.top, width: rect.width, height: rect.height };
}

/** The first of the shell's markers the card's target is found at, else null. */
export function findTargetBox(root: ParentNode, selectors: readonly string[]): Box | null {
  for (const selector of selectors) {
    const box = boxOf(root.querySelector(selector));
    if (box !== null) return box;
  }
  return null;
}

/** Which side of the chrome a control is on: help is at the right end of the title bar, the rest by the name. */
export function chromeSideOf(anchor: ChromeAnchor): "left" | "right" {
  return anchor === "help" ? "right" : "left";
}

/** A card's text as paragraphs: one per blank-line break, each after the first a step down from the last. */
export function textParagraphs(text: string): m.Children[] {
  return text
    .split("\n\n")
    .map((paragraph, index) =>
      m("p", { class: `${index === 0 ? "m-0" : "mt-2 mb-0"} type-body text-primary` }, paragraph),
    );
}

/** What the box shows when it is not a card: the exit badge at the launcher, or the pause notice by the avatar. */
export type TourNotice = "badge" | "paused";

/** Where the box goes for ``subject`` (a card or a notice), given its own size. */
export function placeCard(
  subject: TourCardRecord | TourNotice,
  cardSize: Size,
  root: ParentNode,
  viewport: Size,
): TourPlacement {
  const avatar = findTargetBox(root, tourTargetSelectors("avatar"));
  if (subject === "paused") return placeByAvatar(avatar, cardSize, viewport);
  if (subject === "badge") {
    const launcher = findTargetBox(root, tourTargetSelectors("launcher"));
    return launcher === null ? placeByAvatar(avatar, cardSize, viewport) : placeBeside(launcher, cardSize, viewport);
  }
  const card = subject;
  if (card.chromeAnchor !== null) return placeUnderChrome(chromeSideOf(card.chromeAnchor), cardSize, viewport);
  if (card.target === null || card.target === "avatar") return placeByAvatar(avatar, cardSize, viewport);
  const target = findTargetBox(root, tourTargetSelectors(card.target));
  return target === null ? placeByAvatar(avatar, cardSize, viewport) : placeBeside(target, cardSize, viewport);
}

function arrowStyle(placement: TourPlacement): Record<string, string> | null {
  const arrow = placement.arrow;
  if (arrow === null) return null;
  const offset = `${arrow.offset - ARROW_SIZE_PX / 2}px`;
  switch (arrow.side) {
    case "top":
      return { left: offset, top: ARROW_INSET, borderBottom: "0", borderRight: "0" };
    case "bottom":
      return { left: offset, bottom: ARROW_INSET, borderTop: "0", borderLeft: "0" };
    case "left":
      return { top: offset, left: ARROW_INSET, borderTop: "0", borderRight: "0" };
    case "right":
      return { top: offset, right: ARROW_INSET, borderBottom: "0", borderLeft: "0" };
  }
}

export function TourCard(): m.Component<TourCardAttrs> {
  let size: Size | null = null;
  let followTimer: ReturnType<typeof setInterval> | null = null;
  const onResize = (): void => m.redraw();

  function measure(dom: Element): void {
    const element = dom as HTMLElement;
    const measured = { width: element.offsetWidth, height: element.offsetHeight };
    if (size === null || size.width !== measured.width || size.height !== measured.height) {
      size = measured;
      m.redraw();
    }
  }

  return {
    oncreate(vnode) {
      measure(vnode.dom);
      followTimer = setInterval(() => m.redraw(), FOLLOW_INTERVAL_MS);
      window.addEventListener("resize", onResize);
    },
    onupdate(vnode) {
      measure(vnode.dom);
    },
    onremove() {
      if (followTimer !== null) clearInterval(followTimer);
      followTimer = null;
      window.removeEventListener("resize", onResize);
    },
    view(vnode) {
      const { tour, avatarDesign, openLink } = vnode.attrs;
      const root = vnode.attrs.root ?? document;
      const viewport = vnode.attrs.viewport ?? { width: window.innerWidth, height: window.innerHeight };
      const state = tour.getState();
      const card = tour.shownCard();
      const notice: TourNotice | null =
        state.phase === "exit-badge" ? "badge" : state.phase === "paused" ? "paused" : null;
      const width = card?.image !== null && card?.image !== undefined ? IMAGE_CARD_WIDTH_PX : CARD_WIDTH_PX;
      const cardSize = size ?? { width, height: 160 };
      const placement = placeCard(notice ?? card ?? "badge", { ...cardSize, width }, root, viewport);
      const arrow = arrowStyle(placement);
      const cards = tour.getCards();
      const isLast = state.phase === "card" && state.index === cards.length - 1;
      const text =
        notice === "badge" ? TOUR_EXIT_BADGE_TEXT : notice === "paused" ? TOUR_PAUSED_TEXT : (card?.text ?? "");
      const bullets = notice === null ? (card?.bullets ?? null) : null;
      const link = notice === null ? (card?.link ?? null) : null;
      const image = notice === null ? (card?.image ?? null) : null;
      return m(
        "div",
        {
          role: "dialog",
          "aria-live": "polite",
          "data-tour-card": notice === null ? card?.id : undefined,
          "data-tour-badge": notice === "badge" ? "" : undefined,
          "data-tour-paused": notice === "paused" ? "" : undefined,
          class:
            "tour-card fixed z-(--z-popover) rounded-lg border border-default bg-surface p-4 shadow-overlay " +
            (size === null ? "invisible" : ""),
          style: { left: `${placement.left}px`, top: `${placement.top}px`, width: `${width}px` },
        },
        [
          arrow === null
            ? null
            : m("span", {
                "aria-hidden": "true",
                class: "tour-arrow absolute rotate-45 border border-default bg-surface",
                style: { ...arrow, width: `${ARROW_SIZE_PX}px`, height: `${ARROW_SIZE_PX}px` },
              }),
          m("div", { class: "flex items-start gap-3" }, [
            m("img", {
              src: avatarImageUrl(avatarDesign, "idle", true),
              alt: "",
              draggable: false,
              class: "size-8 shrink-0",
            }),
            m("div", { class: "min-w-0" }, [
              ...textParagraphs(text),
              bullets === null
                ? null
                : m(
                    "ul",
                    { class: "mt-2 mb-0 list-disc pl-5 type-body text-primary" },
                    bullets.map((line) => m("li", line)),
                  ),
            ]),
          ]),
          image === null
            ? null
            : m("img", { src: image.url, alt: image.alt, class: "mt-3 w-full rounded-md bg-fill-hover" }),
          link === null
            ? null
            : m("div", { class: "mt-3 flex justify-end" }, [
                m(
                  "a",
                  {
                    "data-tour-link": "",
                    href: link.url,
                    target: "_blank",
                    rel: "noopener",
                    class: buttonClass("secondary", { sm: true }),
                    onclick: (event: MouseEvent) => {
                      // A plain click goes the shell's way; a modified or middle click keeps the browser's.
                      if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) {
                        return;
                      }
                      event.preventDefault();
                      openLink(link.url);
                    },
                  },
                  link.label,
                ),
              ]),
          notice !== null
            ? m("div", { class: "mt-4 flex justify-end" }, [
                m(
                  Button,
                  {
                    variant: "primary",
                    sm: true,
                    "data-tour-next": "",
                    onclick: () => (notice === "badge" ? tour.dismissBadge() : tour.dismissPause()),
                  },
                  notice === "badge" ? TOUR_EXIT_BADGE_BUTTON_LABEL : TOUR_PAUSED_BUTTON_LABEL,
                ),
              ])
            : m("div", { class: "mt-4 flex items-center justify-between gap-2" }, [
                m(
                  Button,
                  { variant: "ghost", sm: true, "data-tour-exit": "", onclick: () => tour.exit() },
                  "Exit walkthrough",
                ),
                m("div", { class: "flex items-center gap-2" }, [
                  state.phase === "card" && state.index > 0
                    ? m(
                        Button,
                        { variant: "secondary", sm: true, "data-tour-back": "", onclick: () => tour.back() },
                        "Go back",
                      )
                    : null,
                  m(
                    Button,
                    { variant: "primary", sm: true, "data-tour-next": "", onclick: () => tour.next() },
                    card?.buttonLabel ?? (isLast ? "Done" : "Next"),
                  ),
                ]),
              ]),
        ],
      );
    },
  };
}

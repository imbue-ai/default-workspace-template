import m from "mithril";
import { icon } from "@imbue/workspace-ui/src/components/icons";

/** A composer attachment chip's height, which the strip's end fades match. */
export const ATTACHMENT_CHIP_HEIGHT_CLASS = "h-10";

/** The attachment row's edge fade: 4px solid at the edge, then an ease-out falloff over the rest of
 *  the fade box, so there is no visible band where a linear ramp would end. */
const STRIP_FADE_SOLID_PX = 4;

function stripFade(direction: string): string {
  const stops = [0, 0.15, 0.3, 0.45, 0.6, 0.75, 0.9, 1].map((t) => {
    const alpha = Math.round((1 - t) ** 2 * 1000) / 10;
    const at = `calc(${STRIP_FADE_SOLID_PX}px + (100% - ${STRIP_FADE_SOLID_PX}px) * ${t})`;
    return `color-mix(in srgb, var(--c-bg-composer) ${alpha}%, transparent) ${at}`;
  });
  return `linear-gradient(${direction}, var(--c-bg-composer) 0px, ${stops.join(", ")})`;
}

export interface AttachmentStripAttrs {
  children: m.Children;
}

/**
 * The composer's attachments on ONE row: when they don't fit, the row scrolls sideways (scrollbar
 * hidden) and a < / > arrow over an edge fade appears at whichever end has more to show, each
 * paging the row by most of its width.
 *
 * The scroller has 10px of room above and right of the chips (cancelled by a negative top margin)
 * so the remove buttons, centred on the chips' corners, aren't clipped by its overflow. The row is
 * a flow root so that margin stays inside it, keeping the end fades level with the chips.
 */
export function AttachmentStrip(): m.Component<AttachmentStripAttrs> {
  let scroller: HTMLElement | null = null;
  let resizeObserver: ResizeObserver | null = null;
  let edges = { left: false, right: false };
  let chipCount = 0;

  function updateEdges(): void {
    if (scroller === null) return;
    const next = {
      left: scroller.scrollLeft > 1,
      right: scroller.scrollLeft + scroller.clientWidth < scroller.scrollWidth - 1,
    };
    if (next.left !== edges.left || next.right !== edges.right) {
      edges = next;
      m.redraw();
    }
  }

  function page(direction: 1 | -1): void {
    scroller?.scrollBy({ left: direction * scroller.clientWidth * 0.8, behavior: "smooth" });
  }

  function arrow(side: "left" | "right"): m.Children {
    const isLeft = side === "left";
    return [
      m("div", {
        class: `pointer-events-none absolute top-0 ${ATTACHMENT_CHIP_HEIGHT_CLASS} w-8 ${isLeft ? "left-0" : "right-0"}`,
        style: { backgroundImage: stripFade(isLeft ? "to right" : "to left") },
      }),
      // A bare chevron until hovered, when it fills in as a small floating button. It sits 10px
      // outside the row so its hover box doesn't line up with the composer's border.
      m(
        "button",
        {
          type: "button",
          class:
            `message-input-attachments-${side} absolute top-2 z-20 inline-flex h-6 w-6 cursor-pointer items-center ` +
            "justify-center rounded-md border border-transparent text-secondary " +
            "transition-[color,background-color,border-color,box-shadow] hover:border-default hover:bg-surface " +
            "hover:text-primary hover:shadow-[0_2px_6px_rgb(0_0_0/0.12)] " +
            (isLeft ? "-left-2.5" : "-right-2.5"),
          "aria-label": isLeft ? "Scroll attachments left" : "Scroll attachments right",
          onclick: () => page(isLeft ? -1 : 1),
        },
        m.trust(icon(isLeft ? "chevron-left" : "chevron-right", { size: 16, strokeWidth: 2.5 })),
      ),
    ];
  }

  return {
    oncreate(vnode) {
      scroller = (vnode.dom as HTMLElement).firstElementChild as HTMLElement;
      scroller.addEventListener("scroll", updateEdges, { passive: true });
      resizeObserver = new ResizeObserver(updateEdges);
      resizeObserver.observe(scroller);
      chipCount = scroller.childElementCount;
      updateEdges();
    },
    onupdate() {
      // A chip just added goes on the end, so the row scrolls there to show it.
      const count = scroller?.childElementCount ?? 0;
      if (count > chipCount) {
        scroller?.scrollTo({ left: scroller.scrollWidth, behavior: "smooth" });
      }
      chipCount = count;
      updateEdges();
    },
    onremove() {
      scroller?.removeEventListener("scroll", updateEdges);
      resizeObserver?.disconnect();
      scroller = null;
    },
    view(vnode) {
      return m("div", { class: "message-input-attachments relative flow-root pb-1.5" }, [
        m(
          "div",
          {
            class:
              "-mt-2.5 flex gap-1.5 overflow-x-auto pt-2.5 pr-2.5 [scrollbar-width:none] " +
              "[&::-webkit-scrollbar]:hidden",
          },
          vnode.attrs.children,
        ),
        edges.left ? arrow("left") : null,
        edges.right ? arrow("right") : null,
      ]);
    },
  };
}

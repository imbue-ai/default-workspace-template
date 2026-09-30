/**
 * A phone sheet (plan-phone-interface.md): a panel risen from the foot of the screen over a scrim, a grab handle
 * and a heading row at its top. Tapping the scrim dismisses it, and so does dragging its head down past a
 * quarter of its height; a shorter drag springs back. The drag moves the panel straight in the DOM, as a window
 * drag does on the desktop, so nothing redraws per pointer move.
 */

import m from "mithril";

/** How far down the head is dragged, as a fraction of the panel's height, before the release dismisses. */
const DISMISS_FRACTION = 0.25;

export interface SheetAttrs {
  /** ``data-phone-sheet``: which sheet this is. */
  readonly name: "windows" | "start";
  readonly onDismiss: () => void;
  /** The heading row's content, beside which the drag starts. */
  readonly head: m.Children;
}

export function Sheet(): m.Component<SheetAttrs> {
  let panel: HTMLElement | null = null;
  let drag: { readonly pointerId: number; readonly startY: number; offset: number } | null = null;

  function place(offset: number): void {
    if (panel !== null) panel.style.transform = offset === 0 ? "" : `translateY(${offset}px)`;
  }

  return {
    view(vnode) {
      const { name, onDismiss, head } = vnode.attrs;
      const headAttrs: m.Attributes = {
        class: "phone-sheet-head shrink-0 touch-none",
        onpointerdown: (event: PointerEvent) => {
          // A press on a control in the head is the control's, not a drag.
          if ((event.target as Element).closest("button, input, textarea") !== null) return;
          drag = { pointerId: event.pointerId, startY: event.clientY, offset: 0 };
          (event.currentTarget as HTMLElement).setPointerCapture(event.pointerId);
        },
        onpointermove: (event: PointerEvent) => {
          if (drag === null || event.pointerId !== drag.pointerId) return;
          drag.offset = Math.max(0, event.clientY - drag.startY);
          place(drag.offset);
        },
        onpointerup: (event: PointerEvent) => {
          if (drag === null || event.pointerId !== drag.pointerId) return;
          const height = panel?.getBoundingClientRect().height ?? 0;
          const isDismissed = height > 0 && drag.offset > height * DISMISS_FRACTION;
          drag = null;
          place(0);
          if (isDismissed) onDismiss();
        },
        onpointercancel: () => {
          drag = null;
          place(0);
        },
      };
      return m("div", { class: "phone-sheet-layer absolute inset-0 z-(--z-sticky)" }, [
        m("div", {
          "data-phone-sheet-scrim": name,
          class: "phone-sheet-scrim absolute inset-0 animate-[phone-fade_var(--dur-slow)_ease-out] bg-black/30",
          onclick: onDismiss,
        }),
        m(
          "div",
          {
            "data-phone-sheet": name,
            role: "dialog",
            class:
              "phone-sheet absolute inset-x-0 top-(--desk-phone-sheet-top) bottom-0 flex flex-col " +
              "animate-[phone-sheet-rise_var(--dur-slow)_ease-out] rounded-t-(--desk-phone-sheet-radius) bg-page " +
              "shadow-overlay",
            oncreate: (created: m.VnodeDOM) => {
              panel = created.dom as HTMLElement;
            },
            onremove: () => {
              panel = null;
            },
          },
          [
            m("div", headAttrs, [
              m("div", {
                class:
                  "phone-sheet-grab mx-auto mt-2 mb-1 h-(--desk-phone-grab-height) w-(--desk-phone-grab-width) " +
                  "rounded-full bg-strong",
              }),
              head,
            ]),
            vnode.children,
          ],
        ),
      ]);
    },
  };
}

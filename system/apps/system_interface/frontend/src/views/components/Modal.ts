/**
 * The shared modal shell: a dimmed overlay, a centered card, and the
 * header / body / actions regions the workspace's dialogs share. It renders the
 * `.modal-*` class tree (see the "Modal primitive" block in style.css) and wires
 * the one backdrop-dismissal helper (views/modalBackdrop.ts) so every modal
 * dismisses on a primary mousedown that STARTS on the overlay.
 *
 * Escape handling is opt-in through `onEscape` (the shell owns the listener;
 * a dialog that wants no Escape simply omits it). Autofocus stays with each
 * caller: an `oncreate` on the relevant button/input inside `actions` or the
 * body children.
 *
 * Body content is passed as children: `m(Modal, { ... }, [ ...bodyNodes ])`.
 */

import m from "mithril";
import { omitClassAttrs } from "./attrs";
import { backdropDismissAttrs } from "./modalBackdrop";
import { TEXT_BODY_SIZE } from "./typography";

/* Modal shell recipe.
 * The dimmed overlay + centered card + header/title + body copy + actions row
 * the workspace's dialogs share, emitted by the Modal component below. The
 * copy classes (message, label) are used directly by callers for dialog body
 * content. The enter animations' @keyframes (modal-overlay-in / modal-card-in)
 * live in style.css. `.modal-card` also anchors contextual stylesheet rules
 * (the glyph-picker pressed-state feedback). */

const MODAL_OVERLAY_CLASS =
  "modal-overlay fixed inset-0 z-(--z-overlay) flex items-center justify-center bg-black/40 " +
  "animate-[modal-overlay-in_150ms_ease-out]";

const MODAL_CARD_CLASS =
  "modal-card w-[420px] max-w-[90vw] p-6 bg-surface border border-default rounded-lg shadow-overlay " +
  "animate-[modal-card-in_var(--dur-slow)_cubic-bezier(0.16,1,0.3,1)]";

const MODAL_HEADER_CLASS = "modal-header mb-4 flex items-center gap-2";

export const MODAL_TITLE_CLASS = "modal-title m-0 type-heading text-primary";

// Dialog body copy reads at full strength by default -- it is the point of the
// dialog. A genuinely secondary line (a footnote, a hint) opts into
// text-secondary / text-faint at its own call site.
export const MODAL_MESSAGE_CLASS = "modal-message type-body mb-4 text-primary";

export const MODAL_LABEL_CLASS = `modal-label mb-1 block ${TEXT_BODY_SIZE} font-medium text-secondary`;

const MODAL_ACTIONS_CLASS = "modal-actions flex justify-end gap-2";

export interface ModalAttrs {
  // Called when the backdrop is dismissed (a primary mousedown on the overlay).
  onDismiss: () => void;
  // Called on Escape while the modal is up. The shell owns the document
  // listener -- one stable handler added on mount, removed on unmount, redraw
  // after -- so dialogs pass a guard here rather than wiring their own.
  onEscape?: () => void;
  // Card width in px. Defaults to the primitive's 420px when omitted.
  width?: number;
  // Extra attributes merged onto the .modal-card (role, aria-*, an autofocus
  // oncreate, an onclick that stops propagation, ...). Do not pass `style` here;
  // `width` owns the card's inline style. `class`/`className` are dropped: the
  // spread comes after the recipe's class, so a caller's class would silently
  // replace the whole card recipe (same rule as splitAttrs).
  card?: Record<string, unknown>;
  // Extra attributes merged onto the .modal-overlay. `class`/`className` are
  // dropped, as on `card`.
  overlay?: Record<string, unknown>;
  // Convenience header: renders an <h3 class="modal-title"> inside .modal-header.
  title?: m.Children;
  // Custom header content (rendered inside .modal-header), overriding `title` --
  // e.g. an icon + title, or a title + close button.
  header?: m.Children;
  // The right-aligned footer button row.
  actions?: m.Children;
}

// One stable Escape keydown handler: the returned function reads the current
// guard through `getOnEscape` at event time, so a component registers it once
// on mount and `onremove` unregisters that very function even though the guard
// is re-pointed between redraws.
function escapeKeydownHandler(getOnEscape: () => (() => void) | undefined): (event: KeyboardEvent) => void {
  return (event: KeyboardEvent): void => {
    const onEscape = getOnEscape();
    if (event.key !== "Escape" || onEscape === undefined) return;
    onEscape();
    // A document-level listener sits outside mithril's auto-redraw loop.
    m.redraw();
  };
}

// A closure component so each open modal owns ONE stable keydown handler even
// though callers pass a fresh onEscape closure per redraw.
export function Modal(): m.Component<ModalAttrs> {
  let currentOnEscape: (() => void) | undefined;
  const handleKeydown = escapeKeydownHandler(() => currentOnEscape);

  return {
    oncreate() {
      document.addEventListener("keydown", handleKeydown);
    },
    onremove() {
      document.removeEventListener("keydown", handleKeydown);
    },
    view(vnode) {
      const { onDismiss, onEscape, width, card, overlay, title, header, actions } = vnode.attrs;
      currentOnEscape = onEscape;
      const cardAttrs: Record<string, unknown> = omitClassAttrs(card ?? {});
      if (width !== undefined) {
        cardAttrs.style = `width: ${width}px`;
      }
      const headerContent = header ?? (title === undefined ? null : m("h3", { class: MODAL_TITLE_CLASS }, title));
      return m(
        "div",
        { class: MODAL_OVERLAY_CLASS, ...omitClassAttrs(overlay ?? {}), ...backdropDismissAttrs(onDismiss) },
        m("div", { class: MODAL_CARD_CLASS, ...cardAttrs }, [
          headerContent === null || headerContent === undefined
            ? null
            : m("div", { class: MODAL_HEADER_CLASS }, headerContent),
          vnode.children,
          actions === undefined ? null : m("div", { class: MODAL_ACTIONS_CLASS }, actions),
        ]),
      );
    },
  };
}

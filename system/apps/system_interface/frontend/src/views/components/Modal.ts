/**
 * The shared modal shell: a dimmed overlay, a centered card, and the
 * header / body / actions regions the workspace's dialogs share. It renders the
 * `.modal-*` class tree (see the "Modal primitive" block in style.css) and wires
 * the one backdrop-dismissal helper (views/modalBackdrop.ts) so every modal
 * dismisses on a primary mousedown that STARTS on the overlay.
 *
 * Escape handling and autofocus stay with each caller: some dialogs
 * (delete-confirm, share) intentionally have no Escape, and the ones that do own
 * a document-level keydown listener over their own lifecycle. Pass such wiring
 * through `overlay` (an oncreate/onremove that registers the listener), and put
 * any autofocus `oncreate` on the relevant button/input inside `actions` or the
 * body children.
 *
 * Body content is passed as children: `m(Modal, { ... }, [ ...bodyNodes ])`.
 */

import m from "mithril";
import { backdropDismissAttrs } from "./modalBackdrop";
import { TEXT_BODY_SIZE } from "./typography";

/* Modal shell recipe.
 * The dimmed overlay + centered card + header/title + body copy + actions row
 * the workspace's dialogs share, emitted by the Modal component below. The
 * copy classes (message, label) are used directly by callers for dialog body
 * content. The enter animations' @keyframes (modal-overlay-in / modal-card-in)
 * live in style.css. `.modal-card` also anchors contextual stylesheet rules
 * (the glyph-picker pressed-state feedback). */

export const MODAL_OVERLAY_CLASS =
  "modal-overlay fixed inset-0 z-(--z-overlay) flex items-center justify-center bg-black/40 " +
  "animate-[modal-overlay-in_150ms_ease-out]";

export const MODAL_CARD_CLASS =
  "modal-card w-[420px] max-w-[90vw] p-6 bg-surface border border-default rounded-lg shadow-overlay " +
  "animate-[modal-card-in_var(--dur-slow)_cubic-bezier(0.16,1,0.3,1)]";

export const MODAL_HEADER_CLASS = "modal-header mb-4 flex items-center gap-2";

export const MODAL_TITLE_CLASS = "modal-title m-0 type-heading text-primary";

// Dialog body copy reads at full strength by default -- it is the point of the
// dialog. A genuinely secondary line (a footnote, a hint) opts into
// text-secondary / text-faint at its own call site.
export const MODAL_BODY_CLASS = "modal-body type-body text-primary";

export const MODAL_MESSAGE_CLASS = "modal-message type-body mb-4 text-primary";

export const MODAL_LABEL_CLASS = `modal-label mb-1 block ${TEXT_BODY_SIZE} font-medium text-secondary`;

export const MODAL_ACTIONS_CLASS = "modal-actions flex justify-end gap-2";

export interface ModalAttrs {
  // Called when the backdrop is dismissed (a primary mousedown on the overlay).
  onDismiss: () => void;
  // Card width in px. Defaults to the primitive's 420px when omitted.
  width?: number;
  // Extra attributes merged onto the .modal-card (role, aria-*, an autofocus
  // oncreate, an onclick that stops propagation, ...). Do not pass `style` here;
  // `width` owns the card's inline style.
  card?: Record<string, unknown>;
  // Extra attributes merged onto the .modal-overlay -- used for a per-modal
  // Escape listener wired over the overlay's oncreate/onremove.
  overlay?: Record<string, unknown>;
  // Convenience header: renders an <h3 class="modal-title"> inside .modal-header.
  title?: m.Children;
  // Custom header content (rendered inside .modal-header), overriding `title` --
  // e.g. an icon + title, or a title + close button.
  header?: m.Children;
  // The right-aligned footer button row.
  actions?: m.Children;
}

export const Modal: m.Component<ModalAttrs> = {
  view(vnode) {
    const { onDismiss, width, card, overlay, title, header, actions } = vnode.attrs;
    const cardAttrs: Record<string, unknown> = { ...(card ?? {}) };
    if (width !== undefined) {
      cardAttrs.style = `width: ${width}px`;
    }
    const headerContent = header ?? (title === undefined ? null : m("h3", { class: MODAL_TITLE_CLASS }, title));
    return m(
      "div",
      { class: MODAL_OVERLAY_CLASS, ...(overlay ?? {}), ...backdropDismissAttrs(onDismiss) },
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

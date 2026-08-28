import m from "mithril";
import { Button } from "./Button";
import { MODAL_MESSAGE_CLASS, Modal } from "./Modal";
import { hoverTooltipAttrs } from "./hoverTooltip";

/**
 * The workspace's one notice modal: a title, a body, and buttons.
 *
 * Every notice in the chat used to re-type this markup -- the declined-command notice, the auth
 * notice, the send-failure notice, and three dialogs elsewhere. The copies drifted in exactly the
 * way hand-copying drifts: two registered an Escape handler and one did not, so Escape dismissed
 * some notices and not others, and only some focused their first button. Owning the overlay, the
 * Escape listener, the backdrop press and the focus rule in one place is what makes those
 * behaviours the same everywhere rather than the same by coincidence.
 *
 * Dismissal is uniform on purpose: the button, Escape, and a backdrop press all call ``onDismiss``,
 * so a caller cannot make one of them mean something different from the others. A caller that must
 * not be dismissed while it is busy says so with ``isDismissable``.
 */
export interface NoticeAction {
  label: string;
  /** Shown on hover. Say what the button will DO, especially when it is destructive. */
  tooltip?: string;
  /** Destructive actions are styled apart so they are not the easy button to reach. */
  isDestructive?: boolean;
  isDisabled?: boolean;
  run: () => void;
}

export interface NoticeDialogAttrs {
  title: string;
  /** The body paragraphs, in order. Empty entries are dropped. */
  body: readonly (string | null)[];
  /** The dismissive button's label -- "OK" when it is the only choice, "Cancel" when it is not. */
  dismissLabel: string;
  /** Actions beyond dismissal, rendered after it. */
  actions?: readonly NoticeAction[];
  /** False while an action is running, so the notice cannot be closed out from under it. */
  isDismissable?: boolean;
  onDismiss: () => void;
}

/** Body copy, with guards for text this component does not control: the send-failure notice puts
 *  a server's own words in here, and those are not always the one tidy sentence the other callers
 *  pass. A proxy or an unhandled route answers with a whole HTML page, and an unbroken token (a
 *  URL, a traceback line) would otherwise widen the dialog past the viewport or push its OK button
 *  off the bottom. Harmless for fixed text. */
const NOTICE_BODY_CLASS = `${MODAL_MESSAGE_CLASS} wrap-anywhere max-h-[40vh] overflow-y-auto`;

/**
 * Closure state, so the Escape listener registered in ``oncreate`` is the SAME function object
 * ``onremove`` unregisters. A handler defined inside ``view`` is a new closure on every redraw,
 * which ``removeEventListener`` cannot match -- so every redraw would leave another live listener
 * behind, and each one would keep dismissing long after its notice was gone.
 */
export function makeNoticeDialog(): m.Component<NoticeDialogAttrs> {
  let dismiss: () => void = () => {};

  function handleKeydown(event: KeyboardEvent): void {
    if (event.key === "Escape") dismiss();
  }

  return {
    view(vnode) {
      const { title, body, dismissLabel, actions = [], isDismissable = true, onDismiss } = vnode.attrs;
      dismiss = (): void => {
        if (isDismissable) onDismiss();
      };
      return m(
        Modal,
        {
          onDismiss: dismiss,
          overlay: {
            oncreate() {
              document.addEventListener("keydown", handleKeydown);
            },
            onremove() {
              document.removeEventListener("keydown", handleKeydown);
            },
          },
          title,
          actions: [
            m(
              Button,
              {
                // A bare marker (like btn--primary) so tests can find the dismissive button.
                extra: "notice-dismiss",
                // Focused on open: the dismissive button is the only one that does not act, so it
                // is the safe thing for Enter and Space to land on.
                oncreate: (buttonVnode) => (buttonVnode.dom as HTMLButtonElement).focus(),
                disabled: !isDismissable,
                onclick: dismiss,
              },
              dismissLabel,
            ),
            ...actions.map((action) =>
              m(
                Button,
                {
                  // Quiet destructive on purpose: danger text without a fill, so it is styled
                  // apart from the primary action rather than being the easy button to reach.
                  variant: action.isDestructive ? "ghost-destructive" : "primary",
                  ...(action.tooltip === undefined ? {} : hoverTooltipAttrs(action.tooltip)),
                  disabled: action.isDisabled === true,
                  onclick: () => action.run(),
                },
                action.label,
              ),
            ),
          ],
        },
        body
          .filter((line): line is string => line !== null && line !== "")
          .map((line) => m("p", { class: NOTICE_BODY_CLASS }, line)),
      );
    },
  };
}

// Deliberately no shared instance: the factory holds the dismiss handler its Escape listener
// closes over, so two notices sharing one would have the second overwrite the first's. Each
// render site makes its own.

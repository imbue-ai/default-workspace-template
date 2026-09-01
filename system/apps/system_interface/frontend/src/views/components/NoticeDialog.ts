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
 * some notices and not others, and only some focused their first button. Owning the backdrop
 * press, the focus rule, and the Escape guard in one place (with the Escape listener itself on
 * the Modal shell, via ``onEscape``) is what makes those behaviours the same everywhere rather
 * than the same by coincidence.
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
  /** Greys the button and ignores its clicks. The greying is aria-disabled, not the native
   *  ``disabled``, so ``tooltip`` stays reachable while the button cannot be used. */
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

/** Body copy, with wrap/scroll guards for text this component does not control: the send-failure
 *  notice puts a server's own words in here, which can be a whole HTML page or an unbroken token
 *  (a URL, a traceback line) rather than one tidy sentence. */
const NOTICE_BODY_CLASS = `${MODAL_MESSAGE_CLASS} wrap-anywhere max-h-[40vh] overflow-y-auto`;

export function makeNoticeDialog(): m.Component<NoticeDialogAttrs> {
  return {
    view(vnode) {
      const { title, body, dismissLabel, actions = [], isDismissable = true, onDismiss } = vnode.attrs;
      const dismiss = (): void => {
        if (isDismissable) onDismiss();
      };
      return m(
        Modal,
        {
          onDismiss: dismiss,
          onEscape: dismiss,
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
                // aria-disabled, not disabled, like the action buttons below; ``dismiss``
                // itself refuses while the notice is not dismissable.
                ...(isDismissable ? {} : { "aria-disabled": "true" }),
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
                  // aria-disabled, not disabled: a disabled button suppresses the hover/focus
                  // events the tooltip above needs, and the explanation matters most exactly
                  // while the button is greyed (e.g. mid-retry). Clicks are gated here instead.
                  ...(action.isDisabled === true ? { "aria-disabled": "true" } : {}),
                  onclick: () => {
                    if (action.isDisabled !== true) action.run();
                  },
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

// The factory shape predates the Modal shell owning the Escape listener and is kept so render
// sites stay unchanged; instances hold no state anymore.

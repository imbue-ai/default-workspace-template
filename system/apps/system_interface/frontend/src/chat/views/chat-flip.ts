/**
 * The chat card: one conversation, two renderings, and the switch that turns it over.
 *
 * A chat and its agent's terminal are the same conversation shown two ways, so they are the
 * front and back of one surface rather than two places to be. Ported from the mockup's
 * `FlipSurface` (`prototypes/minds-harness`, `ClaudeCodeView.tsx`).
 *
 * Kept out of `ChatPanel` because the two rules below are the whole risk of this feature and
 * both are invisible in review -- a card assembled inline would have them buried in six hundred
 * lines of transcript machinery, and ChatPanel's dependency surface makes it impractical to
 * test. Here they are three arguments and a DOM assertion.
 */
import m from "mithril";

export interface ChatFlipAttrs {
  /** Which face is showing. */
  flipped: boolean;
  /** Whether the back face has ever been shown; see `back`. */
  everFlipped: boolean;
  front: m.Children;
  /** Built lazily, because it starts a terminal session -- a chat that is never turned over
   *  should never attach one. */
  back: () => m.Children;
}

/** The rotating card. The `underBar` is deliberately NOT an argument: it belongs beside this,
 *  never inside it. */
export function chatFlipCard(attrs: ChatFlipAttrs): m.Vnode {
  const { flipped, everFlipped, front, back } = attrs;
  return m(
    "div",
    { class: "chat-flip" },
    m(
      "div",
      {
        class: "chat-flip-inner",
        style: `transform: rotateY(${flipped ? 180 : 0}deg);`,
      },
      [
        // `inert`, not `display: none`. A hidden face keeps its SIZE: ttyd sizes the agent's
        // tmux window to its client viewport (`window-size latest`), so a zero-sized back face
        // hands the agent a zero-column terminal, and a zero-sized front face loses the
        // transcript's scroll offset. `backface-visibility` hides them at no layout cost.
        m("div", { class: "chat-flip-face chat-flip-front", inert: flipped ? "" : undefined }, front),
        // Mounted from the first flip onward and never removed again. Mithril destroys a vnode
        // that becomes null, and destroying this one takes its iframe out of the document --
        // which ENDS the ttyd session rather than hiding it. So the sticky flag is what is
        // stored, and `flipped` only drives the transform.
        everFlipped
          ? m("div", { class: "chat-flip-face chat-flip-back", inert: flipped ? undefined : "" }, back())
          : null,
      ],
    ),
  );
}

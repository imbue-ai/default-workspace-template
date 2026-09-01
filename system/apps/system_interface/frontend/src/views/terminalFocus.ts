/**
 * Host-driven focus for embedded ttyd terminals.
 *
 * The patched ttyd client (mngr_ttyd's vendored web client) never takes focus on its
 * own -- an embedded pane deciding to focus itself is how a background terminal
 * reconnect-looping against a dead tmux session used to steal the composer's focus
 * several times a second. Instead, the HOST decides when the user navigated to a
 * terminal (a tab activation, the chat card flipping to its terminal face) and asks the
 * client to focus via this message.
 */

// Posted more than once because the iframe may still be mounting or its client still
// connecting when the user navigates to it (the client only listens while connected).
// The message is idempotent, and panes that are not ttyd ignore it.
const FOCUS_RETRY_DELAYS_MS = [0, 300, 1000];

/** Ask the first ttyd iframe under ``container`` to focus its terminal. */
export function requestTerminalFocus(container: HTMLElement | null): void {
  if (container === null) return;
  for (const delay of FOCUS_RETRY_DELAYS_MS) {
    setTimeout(() => {
      const frame = container.querySelector("iframe");
      frame?.contentWindow?.postMessage({ type: "ttyd-focus" }, "*");
    }, delay);
  }
}

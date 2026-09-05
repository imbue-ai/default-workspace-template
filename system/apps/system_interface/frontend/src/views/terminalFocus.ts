/**
 * Host-driven focus for embedded ttyd terminals -- one of the sanctioned cross-frame
 * messaging boundaries (see test_embed_ratchets.py).
 *
 * The patched ttyd client (mngr_ttyd's vendored web client) never takes focus on its
 * own -- an embedded pane deciding to focus itself is how a background terminal
 * reconnect-looping against a dead tmux session used to steal the composer's focus
 * several times a second. Instead, the HOST decides when the user navigated to a pane
 * (a tab activation, the chat card flipping to its terminal face) and asks the client
 * to focus via this message. The shell sends it to every pane it activates, knowing
 * nothing about which app is behind it: a pane that is not the patched ttyd client
 * ignores the type.
 *
 * The boundary's whole contract: OUTBOUND only (no listener is ever registered here),
 * exactly one payload-free message shape, addressed only to iframes inside a
 * workspace-owned container element the caller passes. The target origin is "*"
 * because the message carries nothing a third party could use and the pane's
 * proxied origin is not statically knowable.
 */

// Posted more than once because the iframe may still be mounting or its client still
// connecting when the user navigates to it (the client only listens while connected).
// The message is idempotent, and panes that are not ttyd ignore it.
const FOCUS_RETRY_DELAYS_MS = [0, 300, 1000];

/** Ask the first iframe under ``container`` to take focus (a ttyd client focuses its terminal; any other page ignores it). */
export function requestFrameFocus(container: HTMLElement | null): void {
  if (container === null) return;
  for (const delay of FOCUS_RETRY_DELAYS_MS) {
    setTimeout(() => {
      const frame = container.querySelector("iframe");
      frame?.contentWindow?.postMessage({ type: "ttyd-focus" }, "*");
    }, delay);
  }
}

/**
 * Solo mode (the pull-out-window spec, section 7.5): ``/?solo=<window-id>`` asks the shell to show that one
 * window edge to edge and nothing else, which is what a pulled-out window's desktop window loads. The chrome
 * adds ``reopened=1`` when it reopened that desktop window rather than tearing the window out just now (a
 * session restore at launch or when the app is reopened, a backend retry). Both stay in the URL, so any reload of the page (the
 * interface reload, the browser's own) comes back as the same pop-out; this module only reads them.
 */

const SOLO_PARAM = "solo";
const REOPENED_PARAM = "reopened";

export interface SoloMode {
  /** The one window the page shows. */
  readonly windowId: string;
  /** Whether the chrome reopened the page's desktop window rather than opening it for a tear-out just now: such
   *  a page trusts the stored layout at once, since no desktop's detach save can be on its way to it. */
  readonly isReopened: boolean;
}

/** The solo mode a query string asks for, or null when it asks for the whole desktop. */
export function parseSoloMode(search: string): SoloMode | null {
  const params = new URLSearchParams(search);
  const windowId = params.get(SOLO_PARAM);
  if (windowId === null || windowId === "") return null;
  return { windowId, isReopened: params.get(REOPENED_PARAM) === "1" };
}

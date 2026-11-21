// Whether the first run's provider page is still owed to the workspace's owner, as the chat app last said
// (`shell:first-run-provider-step`). While it is, the shell covers the desktop with that page
// (`views/FirstRunCover.ts`) and holds the show-around tour, and tells the embedding Imbue Studio chrome
// (`minds:first-run-provider-step`), which stays in the first run's cream meanwhile and, when the page ends, grows
// the workspace's color over the window; the page comes down under that color.

/** Where the app that owes the page serves it, under its own origin. */
export const FIRST_RUN_PROVIDERS_PATH = "/?view=first-run";

// The origin of the app that said the page is owed, or null while none is.
let pendingOrigin: string | null = null;

export function isFirstRunProviderStepPending(): boolean {
  return pendingOrigin !== null;
}

/** The page the cover frames, or null while none is owed. */
export function firstRunProvidersUrl(): string | null {
  return pendingOrigin === null ? null : `${pendingOrigin}${FIRST_RUN_PROVIDERS_PATH}`;
}

/** Take an app's word for it (`origin` is that app's own); answers whether that changed anything. */
export function setFirstRunProviderStep(isPending: boolean, origin: string): boolean {
  const next = isPending ? origin : null;
  if (next === pendingOrigin) return false;
  pendingOrigin = next;
  return true;
}

/** The embedding chrome, as the step's reports reach it. */
export interface FirstRunChrome {
  report: (isPending: boolean) => void;
  /** How long after the step's end the chrome's color covers the window. */
  coverMs: number;
  schedule: (run: () => void, delayMs: number) => void;
}

// Whether the chrome said it washes the step's end (`washesFirstRunExit`), so the page waits for its color.
let isExitWashed = false;
// Whether the page is waiting out the chrome's color to come down, so a repeated end is not reported twice.
let isTakingDown = false;

/** The chrome said what it does with the step's end; one listening only now hears of a page already up. */
export function noteChromeWashesExit(isWashed: boolean, chrome: FirstRunChrome): void {
  isExitWashed = isWashed;
  if (pendingOrigin !== null) chrome.report(true);
}

/** Take the chat app's word for the step (`origin` is that app's own), tell the chrome, and redraw on a change. */
export function applyFirstRunProviderStep(
  isPending: boolean,
  origin: string,
  chrome: FirstRunChrome,
  redraw: () => void,
): void {
  if (isPending) {
    if (setFirstRunProviderStep(true, origin)) {
      chrome.report(true);
      redraw();
    }
    return;
  }
  if (pendingOrigin === null || isTakingDown) return;
  chrome.report(false);
  const takeDown = (): void => {
    isTakingDown = false;
    if (setFirstRunProviderStep(false, origin)) redraw();
  };
  if (isExitWashed) {
    isTakingDown = true;
    chrome.schedule(takeDown, chrome.coverMs);
  } else {
    takeDown();
  }
}

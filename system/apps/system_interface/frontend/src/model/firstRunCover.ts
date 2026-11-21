// Whether the first run's provider page is still owed to the workspace's owner, as the chat app last said
// (`shell:first-run-provider-step`). While it is, the shell covers the desktop with that page
// (`views/FirstRunCover.ts`) and holds the show-around tour.

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

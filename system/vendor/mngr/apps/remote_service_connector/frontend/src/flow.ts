// Pure helpers for the page flow: where a finished sign-in lands, and how a
// pending authorize handoff is marked as user-confirmed.

// Clamp a ?next= value to a same-host path (mirrors the server-side check;
// anything else falls back to the web client -- the product, not the account
// page. Flows with a real destination (the desktop handoff, share visits,
// the web chrome's own login links) always pass an explicit next.
export function sanitizeNextPath(candidate: string | null): string {
  if (candidate && candidate.startsWith("/") && !candidate.startsWith("//") && !candidate.startsWith("/\\")) {
    return candidate;
  }
  return "/web";
}

export function isAuthorizeNext(next: string): boolean {
  return next.startsWith("/accounts/authorize") || next.startsWith("/share/authorize");
}

// An explicit sign-in (or a clicked "Continue as ...") is the account
// confirmation, so the authorize endpoints receive confirmed=1 and proceed
// without bouncing back here.
export function markNextConfirmed(next: string): string {
  if (!isAuthorizeNext(next)) return next;
  if (next.includes("confirmed=1")) return next;
  return next + (next.includes("?") ? "&" : "?") + "confirmed=1";
}

// What the interstitial / waiting copy calls the thing being authorized.
export function describeNext(next: string): string {
  if (next.startsWith("/share/authorize")) return "open the shared workspace";
  if (next.startsWith("/accounts/authorize")) return "sign in to the Minds app";
  return "continue";
}

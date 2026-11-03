/**
 * The launcher's way to the notifications page (the shell's README, "Notifications on a phone"): the page that turns
 * Web Push on for the device it is opened on. A home-screen app has no address bar, so the launcher is how a phone
 * reaches it. It is offered only where the shell is the top-level page: inside the Imbue Studio app the app's own
 * notifications already reach the user, and a subscription made in its frame would belong to the frame.
 */

import { apiUrl } from "@imbue/workspace-ui/src/base-path";

export const NOTIFICATIONS_LAUNCHER_LABEL = "Notifications on this device";
export const NOTIFICATIONS_LAUNCHER_SEARCH_WORDS: readonly string[] = ["notifications", "notify", "push", "alerts"];
export const NOTIFICATIONS_PAGE_PATH = "/notifications";

/** Whether this window offers the page: only when it is not framed by another page. */
export function isNotificationsPageOffered(
  view: { readonly top: Window | null; readonly self: Window } = window,
): boolean {
  return view.top === view.self;
}

/** Go to the page in this same window, which a home-screen app keeps in its own frame. */
export function openNotificationsPage(location: Pick<Location, "assign"> = window.location): void {
  location.assign(apiUrl(NOTIFICATIONS_PAGE_PATH));
}

/**
 * The launcher's way to the notifications page (the shell's README, "Notifications on a phone"): the page that turns
 * Web Push on for the device it is opened on. A home-screen app has no address bar, so the launcher is how a phone
 * reaches it. It is offered only to the owner (the only one who can turn notifications on), and only where the shell
 * is the top-level page: inside the Imbue Studio app the app's own notifications already reach the user, and a
 * subscription made in its frame would belong to the frame. A phone or tablet is offered it in the menu as it opens;
 * a computer, where the Imbue Studio app's notifications already arrive, only for a search that names it.
 */

import { apiUrl } from "@imbue/workspace-ui/src/base-path";
import { TOUCH_MEDIA_QUERY } from "@imbue/workspace-ui/src/device_queries";
import type { OwnIdentity } from "./Presence";

export const NOTIFICATIONS_LAUNCHER_LABEL = "Notifications on this device";
export const NOTIFICATIONS_LAUNCHER_SEARCH_WORDS: readonly string[] = ["notifications", "notify", "push", "alerts"];
export const NOTIFICATIONS_PAGE_PATH = "/notifications";

/** How the launcher offers the page: not at all, only for a search that names it, or also with no search. */
export type NotificationsOffer = "hidden" | "on-search" | "shown";

/** Whether this window is not framed by another page. */
export function isTopLevelWindow(view: { readonly top: Window | null; readonly self: Window } = window): boolean {
  return view.top === view.self;
}

/** Whether this device is a phone or a tablet: its main pointer is a finger. */
export function isHandheldDevice(matchMedia: (query: string) => { matches: boolean } = window.matchMedia): boolean {
  return matchMedia(TOUCH_MEDIA_QUERY).matches;
}

/** How the launcher offers the page to this viewer, in this window, on this device. */
export function notificationsOfferOf(context: {
  readonly isTopLevel: boolean;
  readonly isOwner: boolean;
  readonly isHandheld: boolean;
}): NotificationsOffer {
  if (!context.isTopLevel || !context.isOwner) return "hidden";
  return context.isHandheld ? "shown" : "on-search";
}

/** How the launcher offers the page in this window, to the viewer ``identity`` names (none until the arrival says
 *  who they are, when it is not offered). */
export function currentNotificationsOffer(identity: OwnIdentity | null): NotificationsOffer {
  if (identity === null || !identity.owner) return "hidden";
  return notificationsOfferOf({ isTopLevel: isTopLevelWindow(), isOwner: true, isHandheld: isHandheldDevice() });
}

/** Go to the page in this same window, which a home-screen app keeps in its own frame. */
export function openNotificationsPage(location: Pick<Location, "assign"> = window.location): void {
  location.assign(apiUrl(NOTIFICATIONS_PAGE_PATH));
}

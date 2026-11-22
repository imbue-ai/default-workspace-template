// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { applyRenderModes } from "../theme/metrics";
import {
  currentNotificationsOffer,
  isHandheldDevice,
  isTopLevelWindow,
  notificationsOfferOf,
  openNotificationsPage,
} from "./notificationsPage";

describe("the notifications page", () => {
  it("knows a window no other page frames", () => {
    const view = {} as Window;
    expect(isTopLevelWindow({ top: view, self: view })).toBe(true);
    expect(isTopLevelWindow({ top: {} as Window, self: {} as Window })).toBe(false);
  });

  it("takes a device whose main pointer is a finger for a phone or tablet", () => {
    const root = document.createElement("html");
    expect(isHandheldDevice(root)).toBe(false);
    applyRenderModes(root, { isPhone: false, isTouch: true });
    expect(isHandheldDevice(root)).toBe(true);
  });

  it("is shown to the owner on a phone, found by search on a computer, and never offered to a visitor", () => {
    expect(notificationsOfferOf({ isTopLevel: true, isOwner: true, isHandheld: true })).toBe("shown");
    expect(notificationsOfferOf({ isTopLevel: true, isOwner: true, isHandheld: false })).toBe("on-search");
    expect(notificationsOfferOf({ isTopLevel: true, isOwner: false, isHandheld: true })).toBe("hidden");
    expect(notificationsOfferOf({ isTopLevel: false, isOwner: true, isHandheld: true })).toBe("hidden");
  });

  it("is not offered before the viewer is known", () => {
    expect(currentNotificationsOffer(null)).toBe("hidden");
  });

  it("opens in this same window, under the shell's root", () => {
    const assigned: string[] = [];
    openNotificationsPage({ assign: (url: string | URL) => void assigned.push(String(url)) });
    expect(assigned).toEqual(["/notifications"]);
  });
});

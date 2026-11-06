// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { isNotificationsPageOffered, openNotificationsPage } from "./notificationsPage";

describe("the notifications page", () => {
  it("is offered only by a window no other page frames", () => {
    const view = {} as Window;
    expect(isNotificationsPageOffered({ top: view, self: view })).toBe(true);
    expect(isNotificationsPageOffered({ top: {} as Window, self: {} as Window })).toBe(false);
  });

  it("opens in this same window, under the shell's root", () => {
    const assigned: string[] = [];
    openNotificationsPage({ assign: (url: string | URL) => void assigned.push(String(url)) });
    expect(assigned).toEqual(["/notifications"]);
  });
});

// @vitest-environment jsdom
import "../testing/dom";

import { afterEach, describe, expect, it, vi } from "vitest";

import m from "mithril";

import { mountView, unmountViews } from "../testing/mount";
import { ReplacedDesktopNotice } from "./ReplacedDesktopNotice";

afterEach(() => {
  unmountViews();
});

describe("ReplacedDesktopNotice", () => {
  it("names the deleted desktop and the one seeded in its place, and dismisses from its one button", () => {
    const onDismiss = vi.fn();
    const root = mountView(() =>
      m(ReplacedDesktopNotice, { replacedDesktopName: "Alice", currentDesktopName: "Alice 2", onDismiss }),
    );
    const card = root.querySelector('[data-replaced-desktop-notice="Alice"]');
    expect(card).not.toBeNull();
    expect(card?.textContent).toContain("Alice 2");
    (card?.querySelector(".replaced-desktop-dismiss") as HTMLButtonElement).click();
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });
});

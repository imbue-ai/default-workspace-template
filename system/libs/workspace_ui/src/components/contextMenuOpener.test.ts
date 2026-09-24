// @vitest-environment jsdom
/**
 * The Mithril opener: it draws the rows the installer hands it as the shared Menu at the
 * pointer, runs a pick, and takes its root down when disposed.
 */
import "../testing/dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MENU_PART_ATTR, MENU_ROW_ATTR } from "./menu";
import { createContextMenuOpener, type ContextMenuOpener } from "./contextMenuOpener";

let opener: ContextMenuOpener | null = null;

afterEach(() => {
  opener?.dispose();
  opener = null;
  document.body.innerHTML = "";
});

describe("createContextMenuOpener", () => {
  it("opens the shared menu with the rows at the point, runs a pick, and disposes cleanly", () => {
    opener = createContextMenuOpener();
    const onSelect = vi.fn();
    opener.open(
      [
        { kind: "action", key: "explain-element", label: "Explain this element...", onSelect },
        { kind: "divider" },
        { kind: "action", key: "copy-element-path", label: "Copy path to element", onSelect: () => undefined },
      ],
      { x: 12, y: 34 },
    );
    const card = document.body.querySelector<HTMLElement>(`[${MENU_PART_ATTR}="menu"]`);
    expect(card).not.toBeNull();
    expect(card!.classList.contains("element-context-menu")).toBe(true);
    expect(opener.menu.isOpen()).toBe(true);
    (document.body.querySelector(`[${MENU_ROW_ATTR}="explain-element"]`) as HTMLElement).click();
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(opener.menu.isOpen()).toBe(false);
    opener.dispose();
    expect(document.body.querySelector("[data-context-menu-root]")).toBeNull();
    opener = null;
  });
});

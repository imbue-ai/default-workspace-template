// @vitest-environment jsdom
import "../testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { KeyboardShortcutsDialog } from "./KeyboardShortcutsDialog";

afterEach(unmountViews);

function card(): HTMLElement {
  return document.querySelector("[data-keyboard-shortcuts-dialog]") as HTMLElement;
}

describe("KeyboardShortcutsDialog", () => {
  it("titles the dialog, holds the keyboard section, and closes from its button", () => {
    const onClose = vi.fn();
    mountView(() => m(KeyboardShortcutsDialog, { onClose }));
    expect(card().textContent).toContain("Keyboard shortcuts");
    // The section is still fetching the catalog when the dialog first paints.
    expect(card().textContent).toContain("Loading the shortcuts...");
    (card().querySelector(".keyboard-shortcuts-close") as HTMLButtonElement).click();
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

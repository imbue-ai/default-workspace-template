// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import m from "mithril";
import { Modal } from "./Modal";

function renderModal(attrs: Record<string, unknown>): HTMLDivElement {
  const root = document.createElement("div");
  m.render(root, m(Modal, { onDismiss: () => {}, ...attrs }, "body"));
  return root;
}

describe("Modal recipe protection", () => {
  it("drops a caller class from card and overlay instead of letting it replace the recipe", () => {
    const root = renderModal({ card: { class: "sneaky-card" }, overlay: { class: "sneaky-overlay" } });
    const card = root.querySelector(".modal-card");
    const overlay = root.querySelector(".modal-overlay");
    // The recipe classes survive -- a caller class would have replaced them outright.
    expect(card).not.toBeNull();
    expect(overlay).not.toBeNull();
    expect(card!.className).not.toContain("sneaky-card");
    expect(overlay!.className).not.toContain("sneaky-overlay");
    m.render(root, []);
  });

  it("still passes card lifecycle hooks and aria attrs through", () => {
    const oncreate = vi.fn();
    const root = renderModal({ card: { oncreate, "aria-label": "Probe" } });
    expect(oncreate).toHaveBeenCalledTimes(1);
    expect(root.querySelector(".modal-card")?.getAttribute("aria-label")).toBe("Probe");
    m.render(root, []);
  });
});

describe("Modal Escape ownership", () => {
  it("fires the latest onEscape, not the one captured at mount", () => {
    const first = vi.fn();
    const second = vi.fn();
    const root = document.createElement("div");
    m.render(root, m(Modal, { onDismiss: () => {}, onEscape: first }, "body"));
    m.render(root, m(Modal, { onDismiss: () => {}, onEscape: second }, "body"));
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledTimes(1);
    m.render(root, []);
  });

  it("does nothing on Escape when no onEscape is given", () => {
    const root = renderModal({});
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    m.render(root, []);
  });
});

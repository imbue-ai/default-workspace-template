// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import m from "mithril";
import { Toasts } from "./toasts";

const TOASTS = [
  { id: 1, message: "Could not move it" },
  { id: 2, message: "Could not copy it" },
];

describe("Toasts", () => {
  it("is a live region even while empty, so a toast added later is announced", () => {
    const root = document.createElement("div");
    m.render(root, m(Toasts, { toasts: [], placementClass: "fixed bottom-4" }));
    const region = root.querySelector('[role="status"]');
    expect(region?.getAttribute("aria-live")).toBe("polite");
    expect(region?.className).toContain("fixed bottom-4");
    expect(root.querySelectorAll("[data-toast]")).toHaveLength(0);
  });

  it("takes no press and has no close button when the page offers no dismissal", () => {
    const root = document.createElement("div");
    m.render(root, m(Toasts, { toasts: TOASTS, placementClass: "" }));
    const toasts = Array.from(root.querySelectorAll<HTMLElement>("[data-toast]"));
    expect(toasts.map((toast) => toast.textContent)).toEqual(["Could not move it", "Could not copy it"]);
    expect(toasts.every((toast) => !toast.className.includes("pointer-events-auto"))).toBe(true);
    expect(root.querySelector("[data-toast-dismiss]")).toBeNull();
  });

  it("gives each toast a close button that dismisses that toast", () => {
    const root = document.createElement("div");
    const onDismiss = vi.fn();
    m.render(root, m(Toasts, { toasts: TOASTS, placementClass: "", onDismiss }));
    const toasts = Array.from(root.querySelectorAll<HTMLElement>("[data-toast]"));
    expect(toasts.every((toast) => toast.className.includes("pointer-events-auto"))).toBe(true);
    toasts[1].querySelector<HTMLButtonElement>("[data-toast-dismiss]")!.click();
    expect(onDismiss).toHaveBeenCalledWith(2);
  });
});

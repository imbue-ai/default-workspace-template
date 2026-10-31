// @vitest-environment jsdom
import "../testing/dom";

import { afterEach, describe, expect, it, vi } from "vitest";
import m from "mithril";

import { ToastQueue } from "../models/toasts";
import { mountView, unmountViews } from "../testing/mount";
import { Toasts } from "./toasts";

const TOASTS = [
  { id: 1, message: "Could not move it", details: null, actions: [], isLasting: false },
  { id: 2, message: "Could not copy it", details: null, actions: [], isLasting: false },
];

afterEach(() => {
  unmountViews();
});

function mountQueue(queue: ToastQueue): HTMLElement {
  return mountView(() =>
    m(Toasts, { toasts: queue.current(), placementClass: "fixed", onDismiss: (id) => queue.dismiss(id) }),
  );
}

describe("Toasts", () => {
  it("is a live region even while empty, so a toast added later is announced", () => {
    const root = document.createElement("div");
    m.render(root, m(Toasts, { toasts: [], placementClass: "fixed bottom-4" }));
    const region = root.querySelector('[role="status"]');
    expect(region?.getAttribute("aria-live")).toBe("polite");
    expect(region?.className).toContain("fixed bottom-4");
    expect(root.querySelectorAll("[data-toast]")).toHaveLength(0);
  });

  it("takes no press and has no close button on a plain toast unless the page asks for one", () => {
    const root = document.createElement("div");
    m.render(root, m(Toasts, { toasts: TOASTS, placementClass: "", onDismiss: vi.fn() }));
    const toasts = Array.from(root.querySelectorAll<HTMLElement>("[data-toast]"));
    expect(toasts.map((toast) => toast.textContent)).toEqual(["Could not move it", "Could not copy it"]);
    expect(toasts.every((toast) => !toast.className.includes("pointer-events-auto"))).toBe(true);
    expect(root.querySelector("[data-toast-dismiss]")).toBeNull();
  });

  it("gives each plain toast a close button that dismisses that toast where the page asks for one", () => {
    const root = document.createElement("div");
    const onDismiss = vi.fn();
    m.render(root, m(Toasts, { toasts: TOASTS, placementClass: "", onDismiss, closesPlainToasts: true }));
    const toasts = Array.from(root.querySelectorAll<HTMLElement>("[data-toast]"));
    expect(toasts.every((toast) => toast.className.includes("pointer-events-auto"))).toBe(true);
    toasts[1].querySelector<HTMLButtonElement>("[data-toast-dismiss]")!.click();
    expect(onDismiss).toHaveBeenCalledWith(2);
  });

  it("gives a lasting note a close control, its buttons, and its details, and takes it away once one is pressed", () => {
    const queue = new ToastQueue(() => m.redraw.sync());
    const retry = vi.fn();
    queue.show("This window couldn't be closed. Try again. If this persists, report a bug.", {
      isLasting: true,
      details: "HTTP 500",
      actions: [{ label: "Try again", run: retry }],
    });
    queue.show("A second note", { isLasting: true, details: "HTTP 502" });
    const root = mountQueue(queue);

    const [first, second] = Array.from(root.querySelectorAll("[data-toast]")) as HTMLElement[];
    expect(first.querySelector(".toast-message")?.textContent).toBe(
      "This window couldn't be closed. Try again. If this persists, report a bug.",
    );
    expect(first.querySelector(".see-details pre")?.textContent).toBe("HTTP 500");
    (first.querySelector(".toast-action") as HTMLButtonElement).click();
    expect(retry).toHaveBeenCalledTimes(1);
    expect(queue.current().map((toast) => toast.message)).toEqual(["A second note"]);

    (second.querySelector(".toast-close") as HTMLButtonElement).click();
    expect(queue.current()).toEqual([]);
  });
});

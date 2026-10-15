// @vitest-environment jsdom
import "../testing/dom";

import { afterEach, describe, expect, it, vi } from "vitest";

import m from "mithril";

import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import { ToastQueue } from "../model/Toasts";
import { Toasts } from "./Toast";

afterEach(() => {
  unmountViews();
});

function mountQueue(queue: ToastQueue): HTMLElement {
  return mountView(() =>
    m(Toasts, { toasts: queue.current(), placementClass: "fixed", onDismiss: (id) => queue.dismiss(id) }),
  );
}

describe("Toasts", () => {
  it("draws a plain note as text alone, with nothing to press", () => {
    const queue = new ToastQueue(() => m.redraw.sync());
    queue.show("Could not move the window: the shell said no");
    const root = mountQueue(queue);
    const toast = root.querySelector("[data-toast]") as HTMLElement;
    expect(toast.textContent).toBe("Could not move the window: the shell said no");
    expect(toast.querySelector("button")).toBeNull();
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

// @vitest-environment jsdom
import "../testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AvatarChooserDialog } from "./AvatarChooserDialog";
import type { AvatarChooserDialogAttrs } from "./AvatarChooserDialog";

afterEach(unmountViews);

function render(overrides: Partial<AvatarChooserDialogAttrs> = {}): HTMLElement {
  const attrs: AvatarChooserDialogAttrs = {
    designs: [
      { id: "gummy-seal", label: "Gummy seal", source_path: null },
      { id: "mine", label: "Mine", source_path: "/tmp/mine.svg" },
    ],
    loadError: null,
    onRetry: vi.fn(),
    selected: "mine",
    onSelect: vi.fn(),
    onDesignOwn: vi.fn(),
    designOwnDisabledReason: "This needs Chat on this desktop.",
    onClose: vi.fn(),
    ...overrides,
  };
  const root = mountView(() => m(AvatarChooserDialog, attrs));
  return root.querySelector("[data-avatar-chooser]") as HTMLElement;
}

describe("AvatarChooserDialog", () => {
  it("lists every design as a still preview with the selection marked, and selects on click", () => {
    const onSelect = vi.fn();
    const dialog = render({ onSelect });
    const cells = dialog.querySelectorAll<HTMLElement>("[data-avatar-design]");
    expect([...cells].map((cell) => cell.getAttribute("data-avatar-design"))).toEqual(["gummy-seal", "mine"]);
    expect(cells[0].getAttribute("aria-pressed")).toBe("false");
    expect(cells[1].getAttribute("aria-pressed")).toBe("true");
    expect(cells[0].querySelector("img")?.getAttribute("src")).toBe(
      "/api/avatars/gummy-seal/image.svg?mood=idle&preview=1",
    );
    cells[0].click();
    expect(onSelect).toHaveBeenCalledWith("gummy-seal");
    const source = dialog.querySelector("[data-avatar-source]") as HTMLAnchorElement;
    expect(source.getAttribute("href")).toBe("/api/avatars/mine/source.svg");
    expect(source.getAttribute("download")).toBe("");
    expect(source.getAttribute("data-hover-tooltip")).toBe("/tmp/mine.svg");
  });

  it("starts the design chat, or says why it cannot", () => {
    const onDesignOwn = vi.fn();
    const button = render({ onDesignOwn }).querySelector(".avatar-design-own") as HTMLButtonElement;
    expect(button.disabled).toBe(false);
    button.click();
    expect(onDesignOwn).toHaveBeenCalledTimes(1);
    const disabled = render({ onDesignOwn: null }).querySelector(".avatar-design-own") as HTMLButtonElement;
    expect(disabled.disabled).toBe(true);
    expect(disabled.parentElement?.getAttribute("data-hover-tooltip")).toBe("This needs Chat on this desktop.");
  });

  it("shows the loading and error states, and closes on Done, which holds the focus when it opens", () => {
    const loading = render({ designs: null });
    expect(loading.querySelector("[role='status']")?.textContent).toBe("Loading designs…");
    expect(loading.querySelector("[data-avatar-source]")).toBeNull();
    // A list that could not be read says so in place of the grid, its reason behind See details, with a Try again.
    const onRetry = vi.fn();
    const failed = render({
      designs: null,
      loadError: {
        message: "Avatar designs couldn't be loaded.",
        details: "HTTP 500",
        actions: [],
        isReportable: false,
      },
      onRetry,
    });
    expect(failed.querySelector(".avatar-designs-failure-message")?.textContent).toBe(
      "Avatar designs couldn't be loaded.",
    );
    expect(failed.querySelector(".see-details pre")?.textContent).toBe("HTTP 500");
    (failed.querySelector(".avatar-designs-failure-action") as HTMLButtonElement).click();
    expect(onRetry).toHaveBeenCalledTimes(1);
    unmountViews();
    const onClose = vi.fn();
    const done = render({ onClose }).querySelector(".avatar-chooser-done") as HTMLElement;
    expect(document.activeElement).toBe(done);
    done.click();
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

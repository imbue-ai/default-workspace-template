// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import m from "mithril";
import { AttachmentStrip } from "./AttachmentStrip";

const CHIP_WIDTH_PX = 100;
const STRIP_WIDTH_PX = 250;

let root: HTMLElement;
let scrollLeft = 0;
let resizeCallbacks: (() => void)[] = [];
const disconnect = vi.fn();
const scrollBy = vi.fn();
const scrollTo = vi.fn();

function chips(count: number): m.Children {
  return Array.from({ length: count }, (_, index) => m("div", { key: `chip-${index}` }, `chip ${index}`));
}

/** The strip's scroller, laid out as jsdom cannot: each chip CHIP_WIDTH_PX wide in a STRIP_WIDTH_PX row. */
function scroller(): HTMLElement {
  return root.querySelector(".message-input-attachments")!.firstElementChild as HTMLElement;
}

function mount(count: number): void {
  m.render(root, m(AttachmentStrip, { children: chips(count) }));
}

function arrows(): string[] {
  return [...root.querySelectorAll("button")].map((button) => button.getAttribute("aria-label") ?? "");
}

beforeEach(() => {
  root = document.createElement("div");
  document.body.append(root);
  scrollLeft = 0;
  resizeCallbacks = [];
  vi.stubGlobal(
    "ResizeObserver",
    class {
      constructor(callback: () => void) {
        resizeCallbacks.push(callback);
      }
      observe(): void {}
      disconnect = disconnect;
    },
  );
  const proto = HTMLElement.prototype as unknown as Record<string, unknown>;
  Object.defineProperty(proto, "clientWidth", { configurable: true, get: () => STRIP_WIDTH_PX });
  Object.defineProperty(proto, "scrollWidth", {
    configurable: true,
    get(this: HTMLElement) {
      return this.childElementCount * CHIP_WIDTH_PX;
    },
  });
  Object.defineProperty(proto, "scrollLeft", {
    configurable: true,
    get: () => scrollLeft,
    set: (value: number) => {
      scrollLeft = value;
    },
  });
  proto.scrollBy = scrollBy;
  proto.scrollTo = scrollTo;
  // The strip redraws when an arrow comes or goes; these tests re-render it by hand instead.
  vi.spyOn(m, "redraw").mockImplementation((() => undefined) as unknown as typeof m.redraw);
});

afterEach(() => {
  m.render(root, null);
  root.remove();
  const proto = HTMLElement.prototype as unknown as Record<string, unknown>;
  for (const name of ["clientWidth", "scrollWidth", "scrollLeft"]) delete proto[name];
  delete proto.scrollBy;
  delete proto.scrollTo;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  disconnect.mockReset();
  scrollBy.mockReset();
  scrollTo.mockReset();
});

describe("AttachmentStrip", () => {
  it("shows no arrows while its chips fit", () => {
    mount(2);
    mount(2);
    expect(arrows()).toEqual([]);
  });

  it("shows the arrow at whichever end has more to show, and both in the middle", () => {
    mount(5);
    mount(5);
    expect(arrows()).toEqual(["Scroll attachments right"]);

    scrollLeft = 120;
    scroller().dispatchEvent(new Event("scroll"));
    mount(5);
    expect(arrows()).toEqual(["Scroll attachments left", "Scroll attachments right"]);

    scrollLeft = 5 * CHIP_WIDTH_PX - STRIP_WIDTH_PX;
    scroller().dispatchEvent(new Event("scroll"));
    mount(5);
    expect(arrows()).toEqual(["Scroll attachments left"]);
  });

  it("pages by most of its width in the arrow's direction", () => {
    scrollLeft = 120;
    mount(5);
    mount(5);
    (root.querySelector('[aria-label="Scroll attachments right"]') as HTMLButtonElement).click();
    expect(scrollBy).toHaveBeenLastCalledWith({ left: STRIP_WIDTH_PX * 0.8, behavior: "smooth" });
    (root.querySelector('[aria-label="Scroll attachments left"]') as HTMLButtonElement).click();
    expect(scrollBy).toHaveBeenLastCalledWith({ left: -STRIP_WIDTH_PX * 0.8, behavior: "smooth" });
  });

  it("scrolls to the end when a chip is added, and not when one goes or nothing changes", () => {
    mount(3);
    mount(3);
    expect(scrollTo).not.toHaveBeenCalled();
    mount(4);
    expect(scrollTo).toHaveBeenCalledWith({ left: 4 * CHIP_WIDTH_PX, behavior: "smooth" });
    scrollTo.mockClear();
    mount(3);
    expect(scrollTo).not.toHaveBeenCalled();
  });

  it("re-checks its ends when it is resized, and lets go of its observer when removed", () => {
    mount(2);
    mount(2);
    expect(arrows()).toEqual([]);
    // Still two chips, but now they overflow: the row narrowed.
    Object.defineProperty(HTMLElement.prototype, "clientWidth", { configurable: true, get: () => 150 });
    for (const callback of resizeCallbacks) callback();
    mount(2);
    expect(arrows()).toEqual(["Scroll attachments right"]);

    m.render(root, null);
    expect(disconnect).toHaveBeenCalled();
  });
});

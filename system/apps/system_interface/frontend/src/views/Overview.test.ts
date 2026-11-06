// @vitest-environment jsdom
import "../testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  appRecord,
  desktopRecord,
  layoutRecord,
  placementRecord,
  themeMetricsRecord,
  windowRecord,
} from "../testing/records";
import { GRID_SHARE, Overview, gridFor, overviewPresentationOf, reorderedForDrop, thumbnailSize } from "./Overview";
import type { OverviewAttrs, OverviewTile } from "./Overview";

const METRICS = themeMetricsRecord();
const BACKDROP = { width: 1000, height: 800 };
const docs = appRecord("docs");
const home = desktopRecord("home", {
  name: "Home",
  windows: [windowRecord("win-1", "docs", "/a", { title: "Plan" }), windowRecord("win-2", "docs", "/b")],
});
const work = desktopRecord("work", { name: "Work", wallpaper: { kind: "bundled", name: "dawn" } });
const gone = desktopRecord("gone", { name: "Gone", owner: "user:user-gone" });

function tile(windowId: string, title: string, isMinimized = false): OverviewTile {
  return {
    window: home.windows.find((window) => window.id === windowId) as OverviewTile["window"],
    app: docs,
    title,
    rect: { x: 50, y: 48, width: 600, height: 560 },
    isMinimized,
  };
}

function render(overrides: Partial<OverviewAttrs> = {}): OverviewAttrs {
  const tiles = overrides.tiles ?? [tile("win-1", "Plan"), tile("win-2", "Docs", true)];
  // The rename state lives with the caller (the App's store), so the fixture plays that part: begin and end
  // mutate the attrs the next redraw reads, the way the store's setter redraws the App.
  const attrs: OverviewAttrs = {
    desktop: home,
    tiles,
    grid: gridFor(
      tiles.map((entry) => entry.rect),
      BACKDROP.width,
      BACKDROP.height * GRID_SHARE,
      METRICS,
    ),
    backdrop: BACKDROP,
    metrics: METRICS,
    ownDesktops: [home, work],
    leftBehind: [],
    apps: [docs],
    layoutOf: (desktopId) => (desktopId === "home" ? layoutRecord([placementRecord("win-1")]) : null),
    confirmingDeleteId: null,
    renamingId: null,
    onRaiseWindow: vi.fn(),
    onSwitchDesktop: vi.fn(),
    onBeginRename: vi.fn((desktopId: string) => {
      (attrs as { renamingId: string | null }).renamingId = desktopId;
    }),
    onEndRename: vi.fn(() => {
      (attrs as { renamingId: string | null }).renamingId = null;
    }),
    onRename: vi.fn(),
    onReorder: vi.fn(),
    onDesktopContextMenu: vi.fn(),
    onConfirmDelete: vi.fn(),
    onKeepDesktop: vi.fn(),
    onNewDesktop: vi.fn(),
    onClose: vi.fn(),
    ...overrides,
  };
  mountView(() => m(Overview, attrs));
  return attrs;
}

/** Type into a rename input the way a person does: set the value and let the input event carry it. */
function typeInto(input: HTMLInputElement, text: string): void {
  input.value = text;
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

function query<T extends Element>(selector: string): T {
  const element = document.querySelector<T>(selector);
  if (element === null) throw new Error(`nothing matches ${selector}`);
  return element;
}

afterEach(unmountViews);

describe("gridFor and thumbnailSize", () => {
  it("picks the column count whose cells scale the windows largest, capped below full size", () => {
    const rects = [1, 2, 3, 4].map(() => ({ x: 0, y: 0, width: 400, height: 300 }));
    const grid = gridFor(rects, 1000, 544, METRICS);
    expect(grid.columns).toBe(2);
    expect(grid.scale).toBeGreaterThan(0.5);
    expect(grid.scale).toBeLessThanOrEqual(0.6);
    expect(gridFor([{ x: 0, y: 0, width: 100, height: 100 }], 1000, 544, METRICS).scale).toBe(0.6);
    expect(gridFor([], 1000, 544, METRICS)).toEqual({ columns: 1, scale: 0.6 });
  });

  it("draws a thumbnail as wide as the token allows, narrower when the row would not fit, in the backdrop's proportion", () => {
    expect(thumbnailSize(BACKDROP, 2, METRICS)).toEqual({ width: 200, height: 160 });
    const narrow = thumbnailSize(BACKDROP, 11, METRICS);
    expect(narrow.width).toBeLessThan(200);
    expect(narrow.height).toBeCloseTo(narrow.width * 0.8);
  });

  it("moves a dragged desktop to the gap it was dropped in, counting the lifted one out", () => {
    const desktops = [desktopRecord("a"), desktopRecord("b"), desktopRecord("c")];
    expect(reorderedForDrop(desktops, { desktopId: "a", gap: 3 })).toEqual(["b", "c", "a"]);
    expect(reorderedForDrop(desktops, { desktopId: "c", gap: 0 })).toEqual(["c", "a", "b"]);
    expect(reorderedForDrop(desktops, { desktopId: "a", gap: 1 })).toEqual(["a", "b", "c"]);
    expect(reorderedForDrop(desktops, { desktopId: "b", gap: 1 })).toEqual(["a", "b", "c"]);
    expect(reorderedForDrop(desktops, { desktopId: "x", gap: 1 })).toEqual(["a", "b", "c"]);
  });
});

describe("overviewPresentationOf", () => {
  it("scales each tiled window's page by the grid, hides the rest, and hides every page while a deletion is confirmed", () => {
    const tiles = [tile("win-1", "Plan")];
    const grid = { columns: 1, scale: 0.4 };
    const shown = overviewPresentationOf(tiles, grid, null);
    expect(shown.scaleOf("win-1")).toBe(0.4);
    expect(shown.scaleOf("win-2")).toBeNull();
    // The confirmation hangs up into the grid, where the pages would cover it.
    const confirming = overviewPresentationOf(tiles, grid, "home");
    expect(confirming.scaleOf("win-1")).toBeNull();
  });
});

describe("Overview", () => {
  it("tiles the active desktop's windows as content boxes the pages lie over, faint when minimized, and raises one on a click", () => {
    const attrs = render();
    const tiles = [...document.querySelectorAll<HTMLElement>("[data-overview-window]")];
    expect(tiles.map((element) => element.getAttribute("data-overview-window"))).toEqual(["win-1", "win-2"]);
    expect(tiles.map((element) => element.getAttribute("data-minimized"))).toEqual(["false", "true"]);
    const frame = query<HTMLElement>('[data-window-id="win-1"]');
    expect(frame.querySelector("[data-window-content]")).not.toBeNull();
    expect(frame.style.width).toBe(`${600 * attrs.grid.scale}px`);
    expect(query<HTMLElement>('[data-window-id="win-2"]').style.opacity).toBe("0.6");
    expect(tiles[1].textContent).toContain("minimized");
    tiles[0].click();
    expect(attrs.onRaiseWindow).toHaveBeenCalledWith("win-1");
    expect(attrs.onClose).toHaveBeenCalledTimes(1);
  });

  it("says when the desktop has no windows, and closes on a click on the bare backdrop", () => {
    const attrs = render({ tiles: [] });
    expect(query("[data-overview-no-windows]").textContent).toBe("No windows open on Home");
    query<HTMLElement>("[data-overview-windows]").click();
    expect(attrs.onClose).toHaveBeenCalledTimes(1);
    query<HTMLElement>("[data-overview]").click();
    expect(attrs.onClose).toHaveBeenCalledTimes(2);
  });

  it("draws a thumbnail per own desktop from its wallpaper and placements, the active one marked, switching on a click", () => {
    const attrs = render();
    const thumbnails = [...document.querySelectorAll<HTMLElement>("[data-overview-desktop]")];
    expect(thumbnails.map((element) => element.getAttribute("data-overview-desktop"))).toEqual(["home", "work"]);
    expect(thumbnails.map((element) => element.getAttribute("data-active"))).toEqual(["true", "false"]);
    expect(thumbnails[1].style.backgroundImage).toContain("/wallpapers/bundled/dawn");
    expect(thumbnails[0].querySelector('[data-overview-mini-window="win-1"]')).not.toBeNull();
    expect(thumbnails[1].querySelector("[data-overview-mini-window]")).toBeNull();
    thumbnails[1].click();
    expect(attrs.onSwitchDesktop).toHaveBeenCalledWith("work");
    expect(attrs.onClose).not.toHaveBeenCalled();
    query<HTMLElement>("[data-overview-new-desktop]").click();
    expect(attrs.onNewDesktop).toHaveBeenCalledTimes(1);
  });

  it("renames on a double-click of the name and Enter, and not on Escape or an unchanged name", () => {
    const attrs = render();
    query<HTMLElement>('[data-overview-desktop-name="work"]').dispatchEvent(
      new MouseEvent("dblclick", { bubbles: true }),
    );
    m.redraw.sync();
    const input = query<HTMLInputElement>('[data-overview-rename="work"]');
    typeInto(input, "  Research ");
    const escape = new KeyboardEvent("keydown", { key: "Escape", bubbles: true });
    const onDocumentKeyDown = vi.fn();
    document.addEventListener("keydown", onDocumentKeyDown);
    input.dispatchEvent(escape);
    document.removeEventListener("keydown", onDocumentKeyDown);
    m.redraw.sync();
    expect(onDocumentKeyDown).not.toHaveBeenCalled();
    expect(document.querySelector("[data-overview-rename]")).toBeNull();
    expect(attrs.onRename).not.toHaveBeenCalled();

    query<HTMLElement>('[data-overview-desktop-name="work"]').dispatchEvent(
      new MouseEvent("dblclick", { bubbles: true }),
    );
    m.redraw.sync();
    const again = query<HTMLInputElement>('[data-overview-rename="work"]');
    typeInto(again, "  Research ");
    again.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    again.dispatchEvent(new FocusEvent("blur"));
    m.redraw.sync();
    expect(attrs.onRename).toHaveBeenCalledWith("work", "Research");

    query<HTMLElement>('[data-overview-desktop-name="work"]').dispatchEvent(
      new MouseEvent("dblclick", { bubbles: true }),
    );
    m.redraw.sync();
    query<HTMLInputElement>('[data-overview-rename="work"]').dispatchEvent(new FocusEvent("blur"));
    expect(attrs.onRename).toHaveBeenCalledTimes(1);
  });

  it("keeps what was typed into the rename input through a redraw, and an Escape after typing commits nothing", () => {
    const attrs = render();
    query<HTMLElement>('[data-overview-desktop-name="work"]').dispatchEvent(
      new MouseEvent("dblclick", { bubbles: true }),
    );
    m.redraw.sync();
    const input = query<HTMLInputElement>('[data-overview-rename="work"]');
    typeInto(input, "Research");
    // The shell redraws constantly (presence pushes, live tiles); the draft must survive every one of them.
    m.redraw.sync();
    expect(query<HTMLInputElement>('[data-overview-rename="work"]').value).toBe("Research");
    const after = query<HTMLInputElement>('[data-overview-rename="work"]');
    after.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    after.dispatchEvent(new FocusEvent("blur"));
    m.redraw.sync();
    expect(attrs.onRename).not.toHaveBeenCalled();
    expect(attrs.onEndRename).toHaveBeenCalled();
  });

  it("reorders the own desktops on a drop, naming every one of them in the new order", () => {
    const attrs = render();
    const slots = [...document.querySelectorAll<HTMLElement>("[data-overview-desktop-slot]")];
    slots[0].dispatchEvent(new Event("dragstart", { bubbles: true }));
    slots[1].getBoundingClientRect = () => ({ left: 300, width: 200 }) as DOMRect;
    const over = new MouseEvent("dragover", { bubbles: true, cancelable: true, clientX: 480 });
    slots[1].dispatchEvent(over);
    expect(over.defaultPrevented).toBe(true);
    m.redraw.sync();
    expect(query("[data-overview-drop-gap]").getAttribute("data-overview-drop-gap")).toBe("2");
    slots[1].dispatchEvent(new MouseEvent("drop", { bubbles: true, cancelable: true }));
    expect(attrs.onReorder).toHaveBeenCalledWith(["work", "home"]);
    // A drop back where it came from asks for nothing.
    slots[0].dispatchEvent(new Event("dragstart", { bubbles: true }));
    slots[0].dispatchEvent(new MouseEvent("drop", { bubbles: true, cancelable: true }));
    expect(attrs.onReorder).toHaveBeenCalledTimes(1);
  });

  it("asks for a thumbnail's menu on a right-click and confirms a deletion in place", () => {
    const attrs = render({ confirmingDeleteId: "work" });
    query<HTMLElement>('[data-overview-desktop="work"]').dispatchEvent(
      new MouseEvent("contextmenu", { bubbles: true, cancelable: true, clientX: 30, clientY: 40 }),
    );
    expect(attrs.onDesktopContextMenu).toHaveBeenCalledWith("work", 30, 40, expect.any(Element));
    const confirmation = query<HTMLElement>('[data-overview-delete-confirmation="work"]');
    expect(confirmation.textContent).toContain("Delete Work? Its windows close for everyone.");
    // Above the thumbnail: the desktops row sits against the bottom of the screen, so hanging below would clip.
    expect(confirmation.className).toContain("bottom-full");
    expect(confirmation.className).not.toContain("top-full");
    (confirmation.querySelector("[data-overview-keep]") as HTMLElement).click();
    expect(attrs.onKeepDesktop).toHaveBeenCalledTimes(1);
    (confirmation.querySelector("[data-overview-confirm-delete]") as HTMLElement).click();
    expect(attrs.onConfirmDelete).toHaveBeenCalledWith("work");
  });

  it("lists the desktops left behind after the plus, under their own rule, not renameable or draggable", () => {
    const attrs = render({ leftBehind: [gone] });
    const leftBehind = query<HTMLElement>("[data-overview-left-behind]");
    expect(leftBehind.textContent).toContain("Left behind");
    expect(leftBehind.querySelector('[data-overview-desktop="gone"]')).not.toBeNull();
    const slot = leftBehind.querySelector('[data-overview-desktop-slot="gone"]') as HTMLElement;
    expect(slot.getAttribute("draggable")).toBeNull();
    (leftBehind.querySelector('[data-overview-desktop-name="gone"]') as HTMLElement).dispatchEvent(
      new MouseEvent("dblclick", { bubbles: true }),
    );
    m.redraw.sync();
    expect(attrs.onBeginRename).not.toHaveBeenCalled();
    expect(document.querySelector("[data-overview-rename]")).toBeNull();
    expect(
      query("[data-overview-new-desktop]").compareDocumentPosition(leftBehind) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    // A drag over the own row lands nowhere among them, whatever their index.
    const own = query<HTMLElement>('[data-overview-desktop-slot="work"]');
    own.dispatchEvent(new Event("dragstart", { bubbles: true }));
    own.getBoundingClientRect = () => ({ left: 300, width: 200 }) as DOMRect;
    own.dispatchEvent(new MouseEvent("dragover", { bubbles: true, cancelable: true, clientX: 310 }));
    m.redraw.sync();
    expect(document.querySelector("[data-overview-drop-gap]")).not.toBeNull();
    expect(leftBehind.querySelector("[data-overview-drop-gap]")).toBeNull();
  });

  it("sizes the thumbnails for the desktops left behind as for the viewer's own, so the row fits the backdrop", () => {
    const many = Array.from({ length: 9 }, (_, index) => desktopRecord(`gone-${index}`, { owner: "user:user-gone" }));
    render({ leftBehind: many });
    const widths = [...document.querySelectorAll<HTMLElement>("[data-overview-desktop]")].map((thumbnail) =>
      parseFloat(thumbnail.style.width),
    );
    expect(widths).toHaveLength(11);
    expect(new Set(widths).size).toBe(1);
    expect(widths[0]).toBeLessThan(METRICS.overviewThumbnailWidth);
  });
});

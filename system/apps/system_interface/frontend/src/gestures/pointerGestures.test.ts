// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { GestureBinding, GestureListener } from "./pointerGestures";
import { PointerGestureSource, bindingForTarget } from "./pointerGestures";

let root: HTMLElement;
let detach: (() => void) | null = null;
let events: string[];

function listener(isDraggable: boolean = true): GestureListener {
  return {
    thresholdPx: () => 4,
    isDraggable: () => isDraggable,
    onBegin: (binding, point) => void events.push(`begin:${describe_(binding)}:${point.x},${point.y}`),
    onMove: (binding, point, delta) =>
      void events.push(`move:${describe_(binding)}:${point.x},${point.y}:${delta.x},${delta.y}`),
    onEnd: (binding, point, delta) =>
      void events.push(`end:${describe_(binding)}:${point.x},${point.y}:${delta.x},${delta.y}`),
    onCancel: (binding) => void events.push(`cancel:${describe_(binding)}`),
    onLongPress: (binding, client) => void events.push(`long:${describe_(binding)}:${client.x},${client.y}`),
  };
}

function describe_(binding: GestureBinding): string {
  switch (binding.kind) {
    case "window-move":
      return `move(${binding.windowId})`;
    case "window-resize":
      return `resize(${binding.windowId},${binding.edge})`;
    case "shortcut":
      return `shortcut(${binding.app}:${binding.launch})`;
    case "taskbar-entry":
      return `entry(${binding.windowId})`;
  }
}

function pointer(type: string, target: Element, x: number, y: number, extra: PointerEventInit = {}): void {
  target.dispatchEvent(
    new PointerEvent(type, {
      bubbles: true,
      clientX: x,
      clientY: y,
      pointerId: 1,
      button: 0,
      // The primary button is held until the release.
      buttons: type === "pointerup" ? 0 : 1,
      pointerType: "mouse",
      ...extra,
    }),
  );
}

beforeEach(() => {
  events = [];
  root = document.createElement("div");
  root.innerHTML =
    '<div data-window-id="win-1"><div data-drag-handle><span id="title">T</span><button data-no-drag id="menu">m</button></div>' +
    '<div data-resize-edge="se" id="edge"></div><div id="content"></div></div>' +
    '<div data-shortcut="docs:new" id="shortcut"><span id="icon"></span></div>' +
    '<button data-taskbar-entry="win-1" id="entry">E</button>';
  root.getBoundingClientRect = () => ({ left: 10, top: 20, width: 1000, height: 800 }) as DOMRect;
  root.setPointerCapture = () => undefined;
  document.body.appendChild(root);
});

afterEach(() => {
  detach?.();
  detach = null;
  root.remove();
});

describe("bindingForTarget", () => {
  it("names the window move, the resize edge, and the shortcut by data attribute", () => {
    expect(bindingForTarget(root.querySelector("#title") as Element)).toEqual({
      kind: "window-move",
      windowId: "win-1",
    });
    expect(bindingForTarget(root.querySelector("#edge") as Element)).toEqual({
      kind: "window-resize",
      windowId: "win-1",
      edge: "se",
    });
    expect(bindingForTarget(root.querySelector("#icon") as Element)).toMatchObject({
      kind: "shortcut",
      app: "docs",
      launch: "new",
    });
    expect(bindingForTarget(root.querySelector("#entry") as Element)).toEqual({
      kind: "taskbar-entry",
      windowId: "win-1",
    });
  });

  it("names nothing for a window's content, a no-drag control, or a bad edge", () => {
    expect(bindingForTarget(root.querySelector("#content") as Element)).toBeNull();
    expect(bindingForTarget(root.querySelector("#menu") as Element)).toBeNull();
    (root.querySelector("#edge") as Element).setAttribute("data-resize-edge", "up");
    expect(bindingForTarget(root.querySelector("#edge") as Element)).toBeNull();
  });
});

describe("PointerGestureSource", () => {
  it("begins a drag past the threshold, in root coordinates, capturing the pointer only then, and ends on release", () => {
    const captured: number[] = [];
    root.setPointerCapture = (pointerId: number) => void captured.push(pointerId);
    detach = new PointerGestureSource().attach(root, listener());
    const title = root.querySelector("#title") as Element;
    pointer("pointerdown", title, 110, 70);
    pointer("pointermove", title, 112, 71);
    expect(events).toEqual([]);
    expect(captured).toEqual([]);
    pointer("pointermove", title, 130, 90);
    expect(captured).toEqual([1]);
    pointer("pointerup", title, 130, 90);
    expect(events).toEqual([
      "begin:move(win-1):120,70",
      "move:move(win-1):120,70:20,20",
      "end:move(win-1):120,70:20,20",
    ]);
  });

  it("a press that never travels is a click, not a gesture", () => {
    detach = new PointerGestureSource().attach(root, listener());
    const title = root.querySelector("#title") as Element;
    pointer("pointerdown", title, 110, 70);
    pointer("pointerup", title, 111, 70);
    expect(events).toEqual([]);
  });

  it("a press released over a live page (no pointerup reaches the root) does not become a drag on the next hover", () => {
    detach = new PointerGestureSource().attach(root, listener());
    const title = root.querySelector("#title") as Element;
    pointer("pointerdown", title, 110, 70);
    // The release landed in the focused window's iframe; the pointer hovers back over the shell.
    pointer("pointermove", title, 150, 70, { buttons: 0 });
    pointer("pointermove", title, 190, 70, { buttons: 0 });
    expect(events).toEqual([]);
    // The next press is a fresh gesture rather than being ignored for a pending one.
    pointer("pointerdown", title, 200, 70);
    pointer("pointermove", title, 240, 70);
    pointer("pointerup", title, 240, 70);
    expect(events).toEqual([
      "begin:move(win-1):230,50",
      "move:move(win-1):230,50:40,0",
      "end:move(win-1):230,50:40,0",
    ]);
  });

  it("a touch press released over a live page does not block the next finger's press", () => {
    detach = new PointerGestureSource().attach(root, listener());
    const title = root.querySelector("#title") as Element;
    pointer("pointerdown", title, 110, 70, { pointerId: 7, pointerType: "touch" });
    pointer("pointerdown", title, 110, 70, { pointerId: 8, pointerType: "touch" });
    pointer("pointermove", title, 150, 70, { pointerId: 8, pointerType: "touch" });
    pointer("pointerup", title, 150, 70, { pointerId: 8, pointerType: "touch" });
    expect(events).toEqual([
      "begin:move(win-1):140,50",
      "move:move(win-1):140,50:40,0",
      "end:move(win-1):140,50:40,0",
    ]);
  });

  it("a second pointer during a drag is ignored", () => {
    detach = new PointerGestureSource().attach(root, listener());
    const title = root.querySelector("#title") as Element;
    pointer("pointerdown", title, 110, 70, { pointerId: 7, pointerType: "touch" });
    pointer("pointermove", title, 150, 70, { pointerId: 7, pointerType: "touch" });
    pointer("pointerdown", title, 300, 70, { pointerId: 8, pointerType: "touch" });
    pointer("pointermove", title, 340, 70, { pointerId: 8, pointerType: "touch" });
    pointer("pointerup", title, 160, 70, { pointerId: 7, pointerType: "touch" });
    expect(events).toEqual([
      "begin:move(win-1):140,50",
      "move:move(win-1):140,50:40,0",
      "end:move(win-1):150,50:50,0",
    ]);
  });

  it("does not begin when the listener says the binding is not draggable", () => {
    detach = new PointerGestureSource().attach(root, listener(false));
    const title = root.querySelector("#title") as Element;
    pointer("pointerdown", title, 110, 70);
    pointer("pointermove", title, 150, 70);
    pointer("pointerup", title, 150, 70);
    expect(events).toEqual([]);
  });

  it("cancels a drag the browser cancels", () => {
    detach = new PointerGestureSource().attach(root, listener());
    const edge = root.querySelector("#edge") as Element;
    pointer("pointerdown", edge, 500, 500);
    pointer("pointermove", edge, 520, 530);
    pointer("pointercancel", edge, 520, 530);
    expect(events).toEqual([
      "begin:resize(win-1,se):510,510",
      "move:resize(win-1,se):510,510:20,30",
      "cancel:resize(win-1,se)",
    ]);
  });

  it("a touch held still is a long press whose release's click is swallowed; one that moves is a drag", () => {
    vi.useFakeTimers();
    try {
      detach = new PointerGestureSource().attach(root, listener());
      const shortcut = root.querySelector("#icon") as Element;
      const clicks: number[] = [];
      shortcut.addEventListener("click", () => clicks.push(1));
      pointer("pointerdown", shortcut, 50, 60, { pointerType: "touch" });
      vi.advanceTimersByTime(600);
      expect(events).toEqual(["long:shortcut(docs:new):50,60"]);
      pointer("pointerup", shortcut, 50, 60, { pointerType: "touch" });
      shortcut.dispatchEvent(new MouseEvent("click", { bubbles: true }));
      expect(clicks).toEqual([]);

      events = [];
      pointer("pointerdown", shortcut, 50, 60, { pointerType: "touch" });
      pointer("pointermove", shortcut, 80, 60, { pointerType: "touch" });
      vi.advanceTimersByTime(600);
      pointer("pointerup", shortcut, 80, 60, { pointerType: "touch" });
      expect(events).toEqual([
        "begin:shortcut(docs:new):70,40",
        "move:shortcut(docs:new):70,40:30,0",
        "end:shortcut(docs:new):70,40:30,0",
      ]);
    } finally {
      vi.useRealTimers();
    }
  });

  it("swallows the click a finished drag would fire", () => {
    detach = new PointerGestureSource().attach(root, listener());
    const title = root.querySelector("#title") as Element;
    const clicks: number[] = [];
    title.addEventListener("click", () => clicks.push(1));
    pointer("pointerdown", title, 110, 70);
    pointer("pointermove", title, 150, 70);
    pointer("pointerup", title, 150, 70);
    title.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(clicks).toEqual([]);
    title.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(clicks).toEqual([1]);
  });

  it("does not hold a drag's click suppression over a later press when no click followed the drag", () => {
    detach = new PointerGestureSource().attach(root, listener());
    const title = root.querySelector("#title") as Element;
    const clicks: number[] = [];
    title.addEventListener("click", () => clicks.push(1));
    pointer("pointerdown", title, 110, 70);
    pointer("pointermove", title, 150, 70);
    pointer("pointerup", title, 150, 70);
    pointer("pointerdown", title, 150, 70);
    pointer("pointerup", title, 150, 70);
    title.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(clicks).toEqual([1]);
  });
});

import { describe, expect, it, vi } from "vitest";
import { backdropDismissAttrs } from "./modalBackdrop";

// The one backdrop-dismissal helper every modal shares. It keys off a mouse
// DOWN on the overlay itself -- never a click (which fires wherever the press
// ended, so selecting text inside a dialog and releasing past its edge would
// throw the dialog away) and never a secondary button (a right-click reaches
// for a context menu, not "close").
describe("backdropDismissAttrs", () => {
  it("dismisses on a primary mouse DOWN that starts on the overlay itself", () => {
    const onDismiss = vi.fn();
    const overlay = {};
    backdropDismissAttrs(onDismiss).onmousedown({
      button: 0,
      target: overlay,
      currentTarget: overlay,
    } as unknown as MouseEvent);
    expect(onDismiss).toHaveBeenCalledOnce();
  });

  it("ignores a secondary (e.g. right-click) press on the overlay", () => {
    const onDismiss = vi.fn();
    const overlay = {};
    backdropDismissAttrs(onDismiss).onmousedown({
      button: 2,
      target: overlay,
      currentTarget: overlay,
    } as unknown as MouseEvent);
    expect(onDismiss).not.toHaveBeenCalled();
  });

  it("ignores a press that starts on a child of the dialog, not the overlay", () => {
    const onDismiss = vi.fn();
    const overlay = {};
    const child = {};
    backdropDismissAttrs(onDismiss).onmousedown({
      button: 0,
      target: child,
      currentTarget: overlay,
    } as unknown as MouseEvent);
    expect(onDismiss).not.toHaveBeenCalled();
  });

  // An overlay scrolls a dialog taller than itself, and a scrollbar that takes room sits inside the overlay's box.
  it("ignores a press on the overlay's own scrollbar, and still dismisses on the backdrop beside it", () => {
    const onDismiss = vi.fn();
    const overlay = { clientWidth: 573, offsetWidth: 588, clientHeight: 448, offsetHeight: 448 };
    const press = (offsetX: number): void =>
      backdropDismissAttrs(onDismiss).onmousedown({
        button: 0,
        target: overlay,
        currentTarget: overlay,
        offsetX,
        offsetY: 100,
      } as unknown as MouseEvent);

    press(580);
    expect(onDismiss).not.toHaveBeenCalled();

    press(40);
    expect(onDismiss).toHaveBeenCalledOnce();
  });
});

import { describe, expect, it, vi } from "vitest";
import { backdropDismissAttrs } from "./modalBackdrop";

// The one backdrop-dismissal helper every modal shares. It keys off a mouse
// DOWN on the overlay itself -- never a click (which fires wherever the press
// ended, so selecting text inside a dialog and releasing past its edge used to
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
});

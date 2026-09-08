// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import m from "mithril";
import type { NoticeAction } from "./NoticeDialog";
import { makeNoticeDialog } from "./NoticeDialog";

/** Render a one-action notice and return the dismiss + action buttons. */
function renderNotice(
  action: Partial<NoticeAction> & { run: () => void },
  isDismissable = true,
): { root: HTMLDivElement; dismissButton: HTMLButtonElement; actionButton: HTMLButtonElement } {
  const root = document.createElement("div");
  m.render(
    root,
    m(makeNoticeDialog(), {
      title: "Title",
      body: ["Body"],
      dismissLabel: "OK",
      isDismissable,
      onDismiss: () => {},
      actions: [{ label: "Retry", ...action }],
    }),
  );
  const buttons = root.querySelectorAll("button");
  if (buttons.length !== 2) throw new Error("expected a dismiss and an action button");
  return { root, dismissButton: buttons[0] as HTMLButtonElement, actionButton: buttons[1] as HTMLButtonElement };
}

describe("NoticeDialog greying", () => {
  it("greys a disabled action via aria-disabled so its tooltip events still fire", () => {
    const { root, actionButton } = renderNotice({ run: vi.fn(), tooltip: "Tries again", isDisabled: true });
    expect(actionButton.getAttribute("aria-disabled")).toBe("true");
    // Never the native disabled: it suppresses the hover/focus events the tooltip needs.
    expect(actionButton.disabled).toBe(false);
    m.render(root, []);
  });

  it("ignores clicks on a greyed action", () => {
    const run = vi.fn();
    const { root, actionButton } = renderNotice({ run, isDisabled: true });
    actionButton.dispatchEvent(new MouseEvent("click"));
    expect(run).not.toHaveBeenCalled();
    m.render(root, []);
  });

  it("runs an enabled action on click", () => {
    const run = vi.fn();
    const { root, actionButton } = renderNotice({ run });
    actionButton.dispatchEvent(new MouseEvent("click"));
    expect(run).toHaveBeenCalledTimes(1);
    m.render(root, []);
  });

  it("greys the dismiss button the same way while not dismissable", () => {
    const { root, dismissButton } = renderNotice({ run: vi.fn() }, false);
    expect(dismissButton.getAttribute("aria-disabled")).toBe("true");
    expect(dismissButton.disabled).toBe(false);
    m.render(root, []);
  });
});

describe("NoticeDialog Escape", () => {
  function renderDismissable(onDismiss: () => void, isDismissable: boolean): HTMLDivElement {
    const root = document.createElement("div");
    m.render(
      root,
      m(makeNoticeDialog(), { title: "Title", body: ["Body"], dismissLabel: "OK", isDismissable, onDismiss }),
    );
    return root;
  }

  it("dismisses on Escape through the Modal shell, and stops when the notice is gone", () => {
    const onDismiss = vi.fn();
    const root = renderDismissable(onDismiss, true);
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    expect(onDismiss).toHaveBeenCalledTimes(1);
    // Tearing the notice down must remove the shell's document listener.
    m.render(root, []);
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it("refuses Escape while not dismissable", () => {
    const onDismiss = vi.fn();
    const root = renderDismissable(onDismiss, false);
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    expect(onDismiss).not.toHaveBeenCalled();
    m.render(root, []);
  });
});

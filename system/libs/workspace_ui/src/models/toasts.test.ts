import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TOAST_DURATION_MS, ToastQueue } from "./toasts";

describe("ToastQueue", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("keeps toasts oldest first, each leaving on its own after the duration", () => {
    vi.useFakeTimers();
    const onChange = vi.fn();
    const queue = new ToastQueue(onChange, 1000);
    queue.show("first");
    vi.advanceTimersByTime(400);
    queue.show("second");
    expect(queue.current().map((toast) => toast.message)).toEqual(["first", "second"]);

    vi.advanceTimersByTime(600);
    expect(queue.current().map((toast) => toast.message)).toEqual(["second"]);
    vi.advanceTimersByTime(400);
    expect(queue.current()).toEqual([]);
    expect(onChange).toHaveBeenCalledTimes(4);
  });

  it("drops a dismissed toast at once, and its timer then changes nothing", () => {
    vi.useFakeTimers();
    const onChange = vi.fn();
    const queue = new ToastQueue(onChange, 1000);
    queue.show("gone");
    queue.dismiss(queue.current()[0].id);
    expect(queue.current()).toEqual([]);
    vi.advanceTimersByTime(1000);
    expect(onChange).toHaveBeenCalledTimes(2);
  });
});

describe("ToastQueue's lasting notes", () => {
  let queue: ToastQueue;

  function messages(): string[] {
    return queue.current().map((toast) => toast.message);
  }

  beforeEach(() => {
    vi.useFakeTimers();
    queue = new ToastQueue(() => undefined);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("lets a plain note go on its own and keeps a lasting one until it is dismissed, whatever each carries", () => {
    queue.show("Could not move the shortcut.");
    queue.show("Could not read the desktops and apps.", { isLasting: true });
    vi.advanceTimersByTime(TOAST_DURATION_MS);
    expect(messages()).toEqual(["Could not read the desktops and apps."]);
    queue.dismiss(queue.current()[0].id);
    expect(messages()).toEqual([]);
  });

  it("replaces a note that says the same rather than stacking it, and keeps one whose details differ", () => {
    queue.show("Docs couldn't open that.", { isLasting: true, details: "HTTP 400\nno account" });
    queue.show("Could not close the window.");
    queue.show("Docs couldn't open that.", { isLasting: true, details: "HTTP 400\nno account" });
    queue.show("Docs couldn't open that.", { isLasting: true, details: "HTTP 400\nchat is gone" });

    expect(queue.current().map((toast) => [toast.message, toast.details])).toEqual([
      ["Could not close the window.", null],
      ["Docs couldn't open that.", "HTTP 400\nno account"],
      ["Docs couldn't open that.", "HTTP 400\nchat is gone"],
    ]);
  });

  it("holds at most three lasting notes, dropping the oldest, and never drops a plain one for them", () => {
    queue.show("A plain note.");
    for (let index = 1; index <= 4; index += 1) {
      queue.show(`Lasting ${index}.`, { isLasting: true });
    }
    expect(messages()).toEqual(["A plain note.", "Lasting 2.", "Lasting 3.", "Lasting 4."]);
  });
});

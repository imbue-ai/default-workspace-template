import { afterEach, describe, expect, it, vi } from "vitest";
import { ToastQueue } from "./toasts";

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

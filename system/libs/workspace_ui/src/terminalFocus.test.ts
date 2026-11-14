// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { requestFrameFocus } from "./terminalFocus";

let container: HTMLElement;
let posted: ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.useFakeTimers();
  container = document.createElement("div");
  const frame = document.createElement("iframe");
  container.appendChild(frame);
  document.body.appendChild(container);
  posted = vi.fn();
  (frame.contentWindow as Window).postMessage = posted as unknown as Window["postMessage"];
});

afterEach(() => {
  container.remove();
  vi.useRealTimers();
});

describe("requestFrameFocus", () => {
  it("posts the grant to the first frame under the container three times over the retry schedule", () => {
    requestFrameFocus(container);
    vi.advanceTimersByTime(0);
    expect(posted).toHaveBeenCalledTimes(1);
    expect(posted).toHaveBeenCalledWith({ type: "ttyd-focus" }, "*");
    vi.advanceTimersByTime(1000);
    expect(posted).toHaveBeenCalledTimes(3);
  });

  it("drops the posts still pending when the function it answers is called", () => {
    const cancel = requestFrameFocus(container);
    vi.advanceTimersByTime(100);
    cancel();
    vi.advanceTimersByTime(2000);
    expect(posted).toHaveBeenCalledTimes(1);
  });

  it("drops each post its caller no longer wants, and posts the later ones it still does", () => {
    let isWanted = true;
    requestFrameFocus(container, { isStillWanted: () => isWanted });
    vi.advanceTimersByTime(100);
    isWanted = false;
    vi.advanceTimersByTime(300);
    expect(posted).toHaveBeenCalledTimes(1);
    isWanted = true;
    vi.advanceTimersByTime(1000);
    expect(posted).toHaveBeenCalledTimes(2);
  });

  it("posts nothing, and answers a function that does nothing, for no container", () => {
    const cancel = requestFrameFocus(null);
    vi.advanceTimersByTime(2000);
    cancel();
    expect(posted).not.toHaveBeenCalled();
  });
});

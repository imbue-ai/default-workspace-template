/** The strip's media-path row (spec section 5.4): the loud alert with Retry while the picture is the screencast, the
 *  quiet status line otherwise, and the Details toggle. */
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ControlDocument } from "./state";
import { Strip } from "./strip";

const NOBODY: ControlDocument = {
  controller: "nobody",
  human_pinned: false,
  human_session_id: null,
  human_name: null,
  agent_id: null,
  agent_name: null,
  waiting: [],
  idle_seconds: null,
  idle_release_seconds: null,
};

function build() {
  const onRetry = vi.fn();
  const onToggleDetails = vi.fn();
  const strip = new Strip({
    onTakeControl: () => undefined,
    onReturnControl: () => undefined,
    pointerLockButton: document.createElement("button"),
    onToggleAudio: () => undefined,
    onRetry,
    onToggleDetails,
  });
  document.body.append(strip.element);
  const alert = strip.element.querySelector<HTMLElement>("[data-screen-alert]");
  const status = strip.element.querySelector<HTMLElement>(".screen-strip__status");
  const retry = strip.element.querySelector<HTMLButtonElement>("[data-screen-retry]");
  const details = strip.element.querySelector<HTMLButtonElement>("[data-screen-details]");
  if (!alert || !status || !retry || !details) throw new Error("the strip is missing a media-path control");
  return { strip, alert, status, retry, details, onRetry, onToggleDetails };
}

afterEach(() => {
  document.body.replaceChildren();
});

describe("Strip", () => {
  it("shows the loud alert in place of the quiet line while the picture is the screencast", () => {
    const { strip, alert, status } = build();
    strip.update(NOBODY, "s1", "connecting", null);
    expect(alert.hidden).toBe(true);
    expect(status.textContent).toBe("Connecting");

    strip.update(NOBODY, "s1", "screencast", null);
    expect(alert.hidden).toBe(false);
    expect(alert.textContent).toContain("Low-quality picture");
    expect(alert.textContent).toContain("Retry now");
    expect(status.textContent).toBe("");

    strip.update(NOBODY, "s1", "connected", null);
    expect(alert.hidden).toBe(true);
    expect(status.textContent).toBe("");
  });

  it("keeps a refused verb's reason on the quiet line beside the alert", () => {
    const { strip, alert, status } = build();
    strip.update(NOBODY, "s1", "screencast", "the human has control");
    expect(alert.hidden).toBe(false);
    expect(status.textContent).toBe("the human has control");
  });

  it("Retry now asks for another try", () => {
    const { strip, retry, onRetry } = build();
    strip.update(NOBODY, "s1", "screencast", null);
    retry.click();
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("Details toggles open and closed, saying so on the button", () => {
    const { details, onToggleDetails } = build();
    expect(details.getAttribute("aria-pressed")).toBe("false");
    details.click();
    expect(details.getAttribute("aria-pressed")).toBe("true");
    details.click();
    expect(details.getAttribute("aria-pressed")).toBe("false");
    expect(onToggleDetails.mock.calls).toEqual([[true], [false]]);
  });
});

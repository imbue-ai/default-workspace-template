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
    isBanner: false,
  });
  document.body.append(strip.element);
  const alert = strip.element.querySelector<HTMLElement>("[data-screen-alert]");
  const status = strip.element.querySelector<HTMLElement>(".screen-strip__status");
  const retry = strip.element.querySelector<HTMLButtonElement>("[data-screen-retry]");
  const details = strip.element.querySelector<HTMLButtonElement>("[data-screen-details]");
  if (!alert || !status || !retry || !details) throw new Error("the strip is missing a media-path control");
  return { strip, alert, status, retry, details, onRetry, onToggleDetails };
}

/** The strip as the banner over the stage (``strip=off``): no Details, no control buttons. */
function buildBanner() {
  const onRetry = vi.fn();
  const strip = new Strip({
    onTakeControl: () => undefined,
    onReturnControl: () => undefined,
    pointerLockButton: document.createElement("button"),
    onToggleAudio: () => undefined,
    onRetry,
    onToggleDetails: () => undefined,
    isBanner: true,
  });
  document.body.append(strip.element);
  const alert = strip.element.querySelector<HTMLElement>("[data-screen-alert]");
  const status = strip.element.querySelector<HTMLElement>(".screen-strip__status");
  const retry = strip.element.querySelector<HTMLButtonElement>("[data-screen-retry]");
  if (!alert || !status || !retry) throw new Error("the banner is missing its alert");
  return { strip, alert, status, retry, onRetry };
}

afterEach(() => {
  document.body.replaceChildren();
});

describe("Strip", () => {
  it("shows the loud alert in place of the quiet line while the picture is the screencast", () => {
    const { strip, alert, status } = build();
    strip.update(NOBODY, "s1", false, "connecting", null);
    expect(alert.hidden).toBe(true);
    expect(status.textContent).toBe("Connecting");

    strip.update(NOBODY, "s1", false, "screencast", null);
    expect(alert.hidden).toBe(false);
    expect(alert.textContent).toContain("Low-quality picture");
    expect(alert.textContent).toContain("Retry now");
    expect(status.textContent).toBe("");

    strip.update(NOBODY, "s1", false, "connected", null);
    expect(alert.hidden).toBe(true);
    expect(status.textContent).toBe("");
  });

  it("keeps a refused verb's reason on the quiet line beside the alert", () => {
    const { strip, alert, status } = build();
    strip.update(NOBODY, "s1", false, "screencast", "the human has control");
    expect(alert.hidden).toBe(false);
    expect(status.textContent).toBe("the human has control");
  });

  it("Retry now asks for another try", () => {
    const { strip, retry, onRetry } = build();
    strip.update(NOBODY, "s1", false, "screencast", null);
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

describe("Strip after a handoff", () => {
  const handedOff: ControlDocument = { ...NOBODY, controller: "human", human_pinned: true, waiting: ["Alice"] };

  it("names the handoff and offers Take control and, to the owner, Give control back", () => {
    const { strip } = build();
    strip.update(handedOff, "s1", true, "connected", null);
    expect(strip.element.textContent).toContain("Control: Handed to you");
    expect(strip.element.querySelector<HTMLElement>("[data-screen-take-control]")?.hidden).toBe(false);
    expect(strip.element.querySelector<HTMLElement>("[data-screen-return-control]")?.hidden).toBe(false);
  });

  it("keeps Give control back from a visitor who has not claimed the handoff", () => {
    const { strip } = build();
    strip.update(handedOff, "s1", false, "connected", null);
    expect(strip.element.querySelector<HTMLElement>("[data-screen-return-control]")?.hidden).toBe(true);
  });
});

describe("Strip as the banner (strip=off)", () => {
  it("carries only the alert and the notices, and none of the control row", () => {
    const { strip } = buildBanner();
    expect(strip.element.classList.contains("screen-strip--banner")).toBe(true);
    expect(strip.element.querySelector("[data-screen-controller]")).toBeNull();
    expect(strip.element.querySelector("[data-screen-take-control]")).toBeNull();
    expect(strip.element.querySelector("[data-screen-mute]")).toBeNull();
    expect(strip.element.querySelector("[data-screen-details]")).toBeNull();
  });

  it("shows only while the screencast alert, a notice or a transient line applies", () => {
    const { strip, alert, status, retry, onRetry } = buildBanner();
    expect(strip.element.hidden).toBe(true);

    strip.update(NOBODY, "s1", false, "connecting", null);
    expect(strip.element.hidden).toBe(true);

    strip.update(NOBODY, "s1", false, "screencast", null);
    expect(strip.element.hidden).toBe(false);
    expect(alert.textContent).toContain("Low-quality picture");
    expect(status.hidden).toBe(true);
    retry.click();
    expect(onRetry).toHaveBeenCalledTimes(1);

    strip.update(NOBODY, "s1", false, "connected", "the human has control");
    expect(strip.element.hidden).toBe(false);
    expect(alert.hidden).toBe(true);
    expect(status.textContent).toBe("the human has control");

    strip.update(NOBODY, "s1", false, "connected", null);
    expect(strip.element.hidden).toBe(true);

    strip.showStatus("Saved to the Screen's desktop");
    expect(strip.element.hidden).toBe(false);
    strip.showStatus("");
    expect(strip.element.hidden).toBe(true);
  });
});

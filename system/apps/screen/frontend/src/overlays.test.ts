/** The agent overlay names a page held for the human and offers to open or drop it (spec section 4.2). */
import { describe, expect, it } from "vitest";

import { Overlays, mayCancelPendingUrl } from "./overlays";
import type { ControlDocument, NekoSessionAnswer, ScreenStateDocument } from "./state";

const OWNER: NekoSessionAnswer = { session_id: "s1", token: "tok-1", name: "Josh", is_owner: true };
const VISITOR: NekoSessionAnswer = { session_id: "s2", token: "tok-2", name: "Mia", is_owner: false };

function state(overrides: Partial<ScreenStateDocument>): ScreenStateDocument {
  return {
    lifecycle: "running",
    install_reason: null,
    stop_reason: null,
    title: "Screen",
    video_id: "watch",
    control: {
      controller: "agent",
      human_pinned: false,
      human_session_id: null,
      human_name: null,
      agent_id: "chat-a",
      agent_name: "Alice",
      waiting: [],
      idle_seconds: 3,
      idle_release_seconds: 57,
    },
    screen_size: { width: 1280, height: 720, rate: 60 },
    chromium: { state: "running", name: "browser-1", pending_url: null },
    ...overrides,
  };
}

describe("Overlays", () => {
  it("dims the stage while an agent drives and hides the held-page line when none is held", () => {
    const overlays = new Overlays(
      () => undefined,
      () => undefined,
      () => undefined,
    );
    overlays.update(state({}), "connected", OWNER, null);
    const dim = overlays.dimElement;
    expect(dim.hidden).toBe(false);
    expect(dim.querySelector<HTMLElement>("[data-screen-pending-url]")?.hidden).toBe(true);
    expect(dim.querySelector<HTMLElement>("[data-screen-cancel-pending-url]")?.hidden).toBe(true);
    expect(dim.textContent).toContain("Alice has control");
    expect(dim.textContent).toContain("Take control");
    expect(dim.textContent).not.toContain("open it");
  });

  it("names the held page, relabels Take control, and Cancel asks the service to drop it", () => {
    let cancelled = 0;
    const overlays = new Overlays(
      () => undefined,
      () => undefined,
      () => {
        cancelled += 1;
      },
    );
    overlays.update(
      state({ chromium: { state: "running", name: "browser-1", pending_url: "http://localhost:3000/app" } }),
      "connected",
      OWNER,
      null,
    );
    const dim = overlays.dimElement;
    const pending = dim.querySelector<HTMLElement>("[data-screen-pending-url]");
    expect(pending?.hidden).toBe(false);
    expect(pending?.textContent).toContain("http://localhost:3000/app");
    expect(dim.textContent).toContain("Take control and open it");
    const cancel = dim.querySelector<HTMLButtonElement>("[data-screen-cancel-pending-url]");
    expect(cancel?.hidden).toBe(false);
    cancel?.click();
    expect(cancelled).toBe(1);
  });

  it("names the held page to a visitor who does not control and offers to open it, but not to drop it", () => {
    const overlays = new Overlays(
      () => undefined,
      () => undefined,
      () => undefined,
    );
    overlays.update(
      state({ chromium: { state: "running", name: "browser-1", pending_url: "http://localhost:3000/app" } }),
      "connected",
      VISITOR,
      null,
    );
    const dim = overlays.dimElement;
    expect(dim.querySelector<HTMLElement>("[data-screen-pending-url]")?.hidden).toBe(false);
    expect(dim.textContent).toContain("Take control and open it");
    expect(dim.querySelector<HTMLButtonElement>("[data-screen-cancel-pending-url]")?.hidden).toBe(true);
  });

  it("offers Cancel to the sessions the service lets drop a held page", () => {
    const agentDrives: ControlDocument = state({}).control;
    expect(mayCancelPendingUrl(agentDrives, OWNER)).toBe(true);
    expect(mayCancelPendingUrl(agentDrives, VISITOR)).toBe(false);
    expect(mayCancelPendingUrl(agentDrives, null)).toBe(false);
    const visitorControls: ControlDocument = {
      ...agentDrives,
      controller: "human",
      human_session_id: VISITOR.session_id,
      human_name: VISITOR.name,
      agent_id: null,
      agent_name: null,
    };
    expect(mayCancelPendingUrl(visitorControls, VISITOR)).toBe(true);
    expect(mayCancelPendingUrl(visitorControls, { ...VISITOR, session_id: "s3" })).toBe(false);
  });

  it("lifts the dim once a human holds control, whatever is held", () => {
    const overlays = new Overlays(
      () => undefined,
      () => undefined,
      () => undefined,
    );
    const human = state({ chromium: { state: "running", name: "browser-1", pending_url: "http://localhost:1/" } });
    human.control = { ...human.control, controller: "human", human_session_id: "s1", human_name: "Josh" };
    overlays.update(human, "connected", OWNER, null);
    expect(overlays.dimElement.hidden).toBe(true);
  });
});

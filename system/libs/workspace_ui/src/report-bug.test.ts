// @vitest-environment jsdom
//
// "Report a bug" against the real embed endpoint: the chrome's capabilities arrive as a message from the page's
// parent (in jsdom a top-level page is its own parent), and a press goes back to it as OPEN_HELP.

import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { EMBEDDER_CAPABILITIES, resetEmbedEndpointForTesting, setEmbedderMessageHandler } from "./embed";
import { canReportBug, openBugReport } from "./report-bug";

function capabilitiesArrive(payload: Record<string, unknown>): void {
  window.dispatchEvent(
    new MessageEvent("message", {
      data: { type: EMBEDDER_CAPABILITIES, canPopOut: false, ...payload },
      source: window,
    }),
  );
}

beforeEach(() => {
  // The page's own handler for the capabilities, as the shell registers one; it starts the endpoint listening.
  setEmbedderMessageHandler(EMBEDDER_CAPABILITIES, () => undefined);
});

afterEach(() => {
  resetEmbedEndpointForTesting();
});

describe("Report a bug", () => {
  it("can be offered only once the embedder says it opens its bug report", () => {
    expect(canReportBug()).toBe(false);
    capabilitiesArrive({});
    expect(canReportBug()).toBe(false);
    capabilitiesArrive({ opensHelp: true });
    expect(canReportBug()).toBe(true);
    capabilitiesArrive({ opensHelp: false });
    expect(canReportBug()).toBe(false);
  });

  it("asks the embedder to open its bug report", async () => {
    const posted: unknown[] = [];
    window.addEventListener("message", (event) => posted.push(event.data));
    openBugReport();
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
    expect(posted).toContainEqual(expect.objectContaining({ type: "minds:open-help" }));
  });
});

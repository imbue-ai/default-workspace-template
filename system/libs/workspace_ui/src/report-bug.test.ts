// @vitest-environment jsdom
//
// "Report a bug" against the real embed endpoint: a press goes to the page's parent (in jsdom a top-level page is its
// own parent) as OPEN_HELP.

import { afterEach, describe, expect, it } from "vitest";
import { resetEmbedEndpointForTesting } from "./embed";
import { openBugReport } from "./report-bug";

afterEach(() => {
  resetEmbedEndpointForTesting();
});

describe("Report a bug", () => {
  it("asks the embedder to open its bug report", async () => {
    const posted: unknown[] = [];
    const record = (event: MessageEvent): void => void posted.push(event.data);
    window.addEventListener("message", record);
    try {
      openBugReport();
      await new Promise<void>((resolve) => setTimeout(resolve, 0));
      expect(posted).toContainEqual(expect.objectContaining({ type: "minds:open-help" }));
    } finally {
      window.removeEventListener("message", record);
    }
  });
});

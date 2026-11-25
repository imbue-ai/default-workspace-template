// @vitest-environment jsdom
//
// "Report a bug" against the real embed endpoint: a press goes to the page's parent (in jsdom a top-level page is its
// own parent) as OPEN_HELP, with the failure it is about as its errorDetails.

import { afterEach, describe, expect, it } from "vitest";
import { MAX_HELP_ERROR_DETAILS_LENGTH, resetEmbedEndpointForTesting } from "./embed";
import type { ReportedFailure } from "./app_contract";
import { openBugReport } from "./report-bug";

afterEach(() => {
  resetEmbedEndpointForTesting();
});

/** What the embedder was sent for one press on "Report a bug" about ``failure``. */
async function openHelpSentFor(failure: ReportedFailure | null): Promise<Record<string, unknown>> {
  const posted: Record<string, unknown>[] = [];
  const record = (event: MessageEvent): void => void posted.push(event.data as Record<string, unknown>);
  window.addEventListener("message", record);
  try {
    openBugReport(failure);
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
  } finally {
    window.removeEventListener("message", record);
  }
  const sent = posted.filter((message) => message.type === "minds:open-help");
  expect(sent).toHaveLength(1);
  return sent[0];
}

describe("Report a bug", () => {
  it("asks the embedder to open its bug report with the failure's sentence and details attached", async () => {
    const sent = await openHelpSentFor({
      message: "This window couldn't be closed. Try again.",
      details: "HTTP 500\nboom",
    });
    expect(sent.errorDetails).toBe("This window couldn't be closed. Try again.\n\nHTTP 500\nboom");
  });

  it("cuts details longer than the embedder takes, keeping their start", async () => {
    const details = `HTTP 500\n${"x".repeat(MAX_HELP_ERROR_DETAILS_LENGTH)}`;
    const sent = await openHelpSentFor({ message: "Docs couldn't save.", details });
    const attached = sent.errorDetails as string;
    expect(attached).toHaveLength(MAX_HELP_ERROR_DETAILS_LENGTH);
    expect(attached.startsWith("Docs couldn't save.\n\nHTTP 500\nxxx")).toBe(true);
    expect(attached.endsWith("\n[...]")).toBe(true);
  });

  it("never cuts a character in half", async () => {
    const message = "Docs couldn't save.";
    // The cut falls between the two halves of the emoji that straddles it.
    const keptLength = MAX_HELP_ERROR_DETAILS_LENGTH - "\n[...]".length;
    const details = `${"x".repeat(keptLength - message.length - 2 - 1)}\u{1F600}${"y".repeat(100)}`;
    const attached = (await openHelpSentFor({ message, details })).errorDetails as string;
    expect(attached.length).toBeLessThanOrEqual(MAX_HELP_ERROR_DETAILS_LENGTH);
    expect(attached.endsWith("x\n[...]")).toBe(true);
  });

  it("opens the bug report with nothing attached when there is no failure to attach", async () => {
    const sent = await openHelpSentFor(null);
    expect(sent).not.toHaveProperty("errorDetails");
  });
});

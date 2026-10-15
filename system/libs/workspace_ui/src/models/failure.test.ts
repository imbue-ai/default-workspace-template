import { describe, expect, it } from "vitest";

import { failureSentence, failureText, notAnsweredSentence, readFailure, type RequestKind } from "./failure";

const CLOSE_FAILED = "This window couldn't be closed";

function answer(status: number, body: unknown, rawText: string | null = null, requestKind: RequestKind = "change") {
  return readFailure({ status, body, rawText, requestKind });
}

describe("failureSentence", () => {
  it("shows the server's sentence as written, offering a bug report only when the server asks for one", () => {
    const told = failureSentence(
      answer(503, { detail: "x", user_facing_message: "Chat is still starting." }),
      CLOSE_FAILED,
    );
    expect(told).toEqual({ message: "Chat is still starting.", details: "HTTP 503\nx", isReportable: false });
    const asking = answer(500, {
      user_facing_message: "Something broke. If this persists, report a bug.",
      reportable: true,
    });
    expect(failureSentence(asking, CLOSE_FAILED).isReportable).toBe(true);
    // The ending alone does not make a sentence reportable: only the server's word does.
    const notAsking = answer(500, { user_facing_message: "Something broke. If this persists, report a bug." });
    expect(failureSentence(notAsking, CLOSE_FAILED).isReportable).toBe(false);
  });

  it("says only that the workspace didn't answer, never prefixed with what failed, for a read and for a change", () => {
    expect(failureSentence(answer(0, null, "Failed to fetch"), CLOSE_FAILED)).toEqual({
      message: notAnsweredSentence("change"),
      details: "Failed to fetch",
      isReportable: false,
    });
    expect(failureSentence(answer(502, null, "Bad Gateway", "read"), CLOSE_FAILED).message).toBe(
      notAnsweredSentence("read"),
    );
  });

  it("names what failed for a refusal with a reason, the reason behind See details and no bug report", () => {
    expect(failureSentence(answer(409, { detail: "Window win-1 is pinned" }), CLOSE_FAILED)).toEqual({
      message: "This window couldn't be closed.",
      details: "HTTP 409\nWindow win-1 is pinned",
      isReportable: false,
    });
  });

  it("asks for a bug report when the server failed with a reason, or answered with none", () => {
    expect(failureSentence(answer(502, { detail: "buddy answered the launch with 500: boom" }), CLOSE_FAILED)).toEqual(
      {
        message: "This window couldn't be closed. Try again. If this persists, report a bug.",
        details: "HTTP 502\nbuddy answered the launch with 500: boom",
        isReportable: true,
      },
    );
    expect(failureSentence(answer(500, null, "<html>oops</html>"), CLOSE_FAILED)).toEqual({
      message: "This window couldn't be closed. Try again. If this persists, report a bug.",
      details: "HTTP 500\n<html>oops</html>",
      isReportable: true,
    });
  });

  it("names the workspace when the surface names nothing of its own", () => {
    expect(failureSentence(answer(400, { detail: "bad path" })).message).toBe("The workspace couldn't do that.");
  });
});

describe("failureText", () => {
  it("keeps the server's own words for a screen not yet worded, and our sentence where it gave none", () => {
    expect(failureText(answer(403, { detail: "This is a preview of a proposed change." }))).toBe(
      "This is a preview of a proposed change.",
    );
    expect(failureText(answer(502, { detail: "x answered 500: boom", user_facing_message: "X broke." }))).toBe(
      "x answered 500: boom",
    );
    expect(failureText(answer(502, { user_facing_message: "X broke." }))).toBe("X broke.");
    expect(failureText(answer(0, null, "Failed to fetch"))).toBe(notAnsweredSentence("change"));
    expect(failureText(answer(500, null, "<html>oops</html>"))).toBe(
      "The workspace couldn't do that. Try again. If this persists, report a bug.",
    );
  });
});

import { describe, expect, it } from "vitest";

import { notAnsweredSentence, readFailure } from "./failure";
import { HttpError } from "./http";
import {
  describeRequestError,
  describeRequestErrorKind,
  describeRequestFailure,
  isUnansweredRequest,
  sendFailureKindOf,
} from "./request-error";

const FALLBACK = "The workspace couldn't do that. Try again. If this persists, report a bug.";

/** A rejection the way mithril builds one: its message the raw body text, the status as ``code``, the parsed body
 *  (null when it could not be read) as ``response``. */
function rejection(code: number, response: unknown, rawText: string): Error {
  return Object.assign(new Error(rawText), { code, response });
}

describe("describeRequestFailure", () => {
  it("reads a mithril rejection by the same precedence as the REST helper", () => {
    const userFacing = rejection(
      502,
      { detail: "chat answered the launch with 503", user_facing_message: "Chat is still starting." },
      "{}",
    );
    expect(describeRequestFailure(userFacing)).toMatchObject({
      cause: "user_facing",
      userFacingMessage: "Chat is still starting.",
      isReportable: false,
      details: "HTTP 502\nchat answered the launch with 503",
    });
    const reason = rejection(404, { detail: "Agent 'x' not found", kind: "chat_gone" }, "{}");
    expect(describeRequestFailure(reason)).toMatchObject({
      cause: "reason",
      reason: "Agent 'x' not found",
      kind: "chat_gone",
      details: "HTTP 404\nAgent 'x' not found",
    });
    expect(describeRequestFailure(rejection(503, { detail: "not ready yet" }, "{}")).cause).toBe("reason");
    expect(describeRequestFailure(rejection(500, { oops: true }, '{"oops": true}'))).toMatchObject({
      cause: "no_reason",
      details: 'HTTP 500\n{"oops": true}',
    });
  });

  it("reads a proxy's plain-text gateway status, a dropped connection and a timeout as no answer", () => {
    // Mithril's message for a body it could not parse is the word "null", which goes nowhere.
    const gateway = rejection(502, null, "Backend connection lost");
    expect(describeRequestFailure(gateway)).toMatchObject({
      cause: "not_answered",
      details: "HTTP 502\nBackend connection lost",
    });
    expect(describeRequestFailure(rejection(0, null, String(null)))).toMatchObject({
      cause: "not_answered",
      status: 0,
      details: null,
    });
    expect(describeRequestFailure(rejection(0, undefined, "Request timed out"), "read")).toMatchObject({
      cause: "not_answered",
      requestKind: "read",
      details: "Request timed out",
    });
  });

  it("passes an HttpError through as the REST helper read it, taking the caller's word on what it asked for", () => {
    const error = new HttpError(readFailure({ status: 504, body: null, rawText: "", requestKind: "change" }));
    expect(describeRequestFailure(error)).toBe(error.failure);
    expect(describeRequestFailure(error, "read")).toEqual({ ...error.failure, requestKind: "read" });
    expect(describeRequestError(error, "read")).toBe(notAnsweredSentence("read"));
  });

  it("reads the message of an error the code threw as a reason, with no status", () => {
    expect(describeRequestFailure(new Error("Chat creation returned no chat id"))).toMatchObject({
      cause: "reason",
      reason: "Chat creation returned no chat id",
      status: null,
      details: "Chat creation returned no chat id",
    });
  });
});

describe("describeRequestError", () => {
  it("keeps the server's own words for a screen not yet worded, never prefixing an unanswered request", () => {
    expect(describeRequestError(rejection(0, null, "null"))).toBe(notAnsweredSentence("change"));
    expect(describeRequestError(rejection(409, { detail: "busy" }, "{}"))).toBe("busy");
    expect(describeRequestError(new Error("Upload failed (HTTP 500)"))).toBe("Upload failed (HTTP 500)");
    expect(describeRequestError(rejection(500, null, "null"))).toBe(FALLBACK);
  });

  it("never returns an empty sentence, whatever it is handed", () => {
    expect(describeRequestError(undefined)).toBe(FALLBACK);
    expect(describeRequestError({})).toBe(FALLBACK);
    expect(describeRequestError("   ")).toBe(FALLBACK);
  });
});

describe("describeRequestErrorKind", () => {
  it("reads an unconfirmed submission as its own kind", () => {
    const error = Object.assign(new Error("{}"), {
      code: 500,
      response: { detail: "Timeout waiting for message submission evidence", kind: "submission_unconfirmed" },
    });
    expect(describeRequestErrorKind(error)).toBe("submission_unconfirmed");
  });

  it("reads a kind it does not know as unknown", () => {
    const error = Object.assign(new Error("{}"), { code: 500, response: { detail: "x", kind: "a_kind_added_later" } });
    expect(describeRequestErrorKind(error)).toBe("unknown");
  });

  it("reads a failure with no kind as unknown", () => {
    expect(describeRequestErrorKind(Object.assign(new Error("null"), { code: 503, response: null }))).toBe("unknown");
    expect(describeRequestErrorKind("a plain string")).toBe("unknown");
    expect(describeRequestErrorKind(null)).toBe("unknown");
  });
});

describe("isUnansweredRequest", () => {
  it("reads a request that got no HTTP response as unanswered", () => {
    expect(isUnansweredRequest(Object.assign(new Error(String(null)), { code: 0, response: null }))).toBe(true);
  });

  it("reads a gateway that gave up without the app's own detail as unanswered", () => {
    for (const code of [502, 504]) {
      expect(isUnansweredRequest(Object.assign(new Error(String(null)), { code, response: null }))).toBe(true);
    }
  });

  it("reads a 502 or 504 carrying the app's own detail as answered", () => {
    for (const code of [502, 504]) {
      const error = Object.assign(new Error("{}"), { code, response: { detail: "the agent did not answer" } });
      expect(isUnansweredRequest(error)).toBe(false);
    }
  });

  it("reads any other status, and anything that is not a request error, as answered", () => {
    expect(isUnansweredRequest(Object.assign(new Error("null"), { code: 503, response: null }))).toBe(false);
    expect(isUnansweredRequest(Object.assign(new Error("{}"), { code: 500, response: { detail: "x" } }))).toBe(false);
    expect(isUnansweredRequest(null)).toBe(false);
    expect(isUnansweredRequest("a plain string")).toBe(false);
  });
});

describe("sendFailureKindOf", () => {
  it("keeps a kind it knows and reads anything else as unknown", () => {
    expect(sendFailureKindOf("input_blocked")).toBe("input_blocked");
    expect(sendFailureKindOf("failed")).toBe("unknown");
    expect(sendFailureKindOf(undefined)).toBe("unknown");
  });
});

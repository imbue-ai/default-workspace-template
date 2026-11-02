import { describe, expect, it } from "vitest";

import { describeRequestError, describeRequestErrorKind, isUnansweredRequest } from "./request-error";

// Every case here is a shape mithril actually rejects with; the point of the
// helper is that none of them can reach a user as a bare "null".
describe("describeRequestError", () => {
  it("prefers the server's own detail", () => {
    const error = Object.assign(new Error("{}"), { code: 404, response: { detail: "Agent 'x' not found" } });
    expect(describeRequestError(error)).toBe("Agent 'x' not found");
  });

  it("falls back to the status when the body could not be read", () => {
    // A proxy's plain-text 503 under `responseType: "json"`: mithril cannot read
    // the body, so it builds `new Error(null)` -- message is the word "null".
    const error = Object.assign(new Error(String(null)), { code: 503, response: null });
    expect(describeRequestError(error)).toBe("request failed (HTTP 503)");
  });

  it("keeps a message that says something, even alongside a status", () => {
    const error = Object.assign(new Error("Backend not yet available"), { code: 503, response: null });
    expect(describeRequestError(error)).toBe("Backend not yet available");
  });

  it("keeps mithril's timeout message rather than the unreachable-workspace fallback", () => {
    // What `xhr.ontimeout` builds when the transcript fetch's own 30s cap fires:
    // a real message, `code` 0 (nothing was received), and no `response` at all.
    // A request answered too slowly and a request nothing answered are different
    // things to be told, so the message has to win over the code-0 fallback.
    const error = Object.assign(new Error("Request timed out"), { code: 0 });
    expect(describeRequestError(error)).toBe("Request timed out");
  });

  it("names the unreachable workspace when the request never got a response", () => {
    // Code 0 is the other half of a dead tunnel, and the more common one: nothing
    // answers at all, so there is no status to report. Both the message and the
    // status are uninformative here, and "unknown error" would throw away the
    // one thing that is known.
    const error = Object.assign(new Error(String(null)), { code: 0, response: null });
    expect(describeRequestError(error)).toBe("could not reach the workspace");
  });

  it("never returns an empty string, whatever it is handed", () => {
    expect(describeRequestError(undefined)).not.toBe("");
    expect(describeRequestError({})).not.toBe("");
    expect(describeRequestError("   ")).not.toBe("");
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

  it("reads a kind the chat has no handling for as unknown", () => {
    // agent_not_found means nothing was typed, so the unknown kind's plain Retry fits it.
    for (const kind of ["agent_not_found", "a_kind_added_later"]) {
      const error = Object.assign(new Error("{}"), { code: 500, response: { detail: "x", kind } });
      expect(describeRequestErrorKind(error)).toBe("unknown");
    }
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

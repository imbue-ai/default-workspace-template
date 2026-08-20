import { describe, expect, it } from "vitest";

import { describeRequestError } from "./request-error";

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

  it("never returns an empty string, whatever it is handed", () => {
    expect(describeRequestError(undefined)).not.toBe("");
    expect(describeRequestError({})).not.toBe("");
    expect(describeRequestError(Object.assign(new Error(String(null)), { code: 0 }))).not.toBe("");
  });
});

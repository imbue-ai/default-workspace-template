import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import { describeRequestError, describeRequestErrorKind } from "./request-error";

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

// The mirror is hand-written, and nothing but this test ties it to the enum it mirrors.
// A kind mngr can send that is missing here does not read as itself -- it reads as
// "unknown", which is not a neutral default: callers offer Retry (and a destructive
// Force) for it, which is the wrong answer for a refusal that resending cannot clear.
describe("SendFailureKind mirrors mngr's enum", () => {
  it("names every kind the vendored mngr can send", () => {
    const enumSource = readFileSync(
      resolve(__dirname, "../../../../vendor/mngr/libs/mngr/imbue/mngr/errors.py"),
      "utf8",
    );
    const enumBody = enumSource.split("class SendFailureKind(StrEnum):")[1]?.split("\nclass ")[0];
    expect(enumBody, "SendFailureKind not found in the vendored mngr").toBeDefined();
    const vendoredKinds = [...enumBody!.matchAll(/^\s{4}[A-Z_]+ = "([a-z_]+)"/gm)].map((match) => match[1]);
    expect(vendoredKinds.length).toBeGreaterThan(1);

    // Asserted through the public helper, and one-directionally: the vendored copy is
    // refreshed asynchronously, so a kind this mirror knows and the vendored enum does not
    // is fine. The direction that matters is a kind mngr can send arriving as "unknown".
    for (const kind of vendoredKinds) {
      expect(describeRequestErrorKind({ response: { kind } }), `mngr can send "${kind}"`).toBe(kind);
    }
  });
});

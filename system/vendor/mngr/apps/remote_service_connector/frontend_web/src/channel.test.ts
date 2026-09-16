// The channel cookie's read side: what the claim body ends up carrying for a
// given `document.cookie`, including the values a hand-edited or stale cookie
// can hold.

import { describe, expect, it } from "vitest";
import { normalizeWebChannel, parseWebChannelCookie } from "./channel";

describe("normalizeWebChannel", () => {
  it("accepts every known channel, trimmed and case-insensitively", () => {
    expect(normalizeWebChannel("alpha")).toBe("alpha");
    expect(normalizeWebChannel(" Beta ")).toBe("beta");
    expect(normalizeWebChannel("STABLE")).toBe("stable");
  });

  it("reads anything unknown or missing as stable", () => {
    expect(normalizeWebChannel("nightly")).toBe("stable");
    expect(normalizeWebChannel("")).toBe("stable");
    expect(normalizeWebChannel(null)).toBe("stable");
    expect(normalizeWebChannel(undefined)).toBe("stable");
  });
});

describe("parseWebChannelCookie", () => {
  it("finds the channel cookie among other cookies", () => {
    expect(
      parseWebChannelCookie(
        "sAccessToken=abc; minds_web_channel=alpha; other=1",
      ),
    ).toBe("alpha");
  });

  it("is stable when the cookie is absent", () => {
    expect(parseWebChannelCookie("")).toBe("stable");
    expect(parseWebChannelCookie("sAccessToken=abc")).toBe("stable");
  });

  it("ignores a cookie whose name merely ends in the channel cookie's name", () => {
    expect(parseWebChannelCookie("x_minds_web_channel=alpha")).toBe("stable");
  });

  it("decodes a percent-encoded value and still refuses unknown channels", () => {
    expect(parseWebChannelCookie("minds_web_channel=beta%20")).toBe("beta");
    expect(parseWebChannelCookie("minds_web_channel=%3Cscript%3E")).toBe(
      "stable",
    );
  });

  it("reads a malformed percent sequence as stable instead of throwing", () => {
    expect(parseWebChannelCookie("minds_web_channel=%E0%A4%A")).toBe("stable");
    expect(parseWebChannelCookie("minds_web_channel=%")).toBe("stable");
  });
});

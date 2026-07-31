import { describe, expect, it } from "vitest";

import { deriveServiceOrigin } from "./origin";

describe("deriveServiceOrigin", () => {
  it("nests the service's origin label as a hostname label on a local workspace host", () => {
    expect(
      deriveServiceOrigin("terminal-x7k9q2w1", "host-0af1b2c3d4e5f60718293a4b5c6d7e8f.localhost:8421", "http:"),
    ).toBe("http://terminal-x7k9q2w1.host-0af1b2c3d4e5f60718293a4b5c6d7e8f.localhost:8421/");
  });

  it("handles a local workspace host without a port", () => {
    expect(deriveServiceOrigin("terminal-x7k9q2w1", "host-0af1b2c3d4e5f60718293a4b5c6d7e8f.localhost", "http:")).toBe(
      "http://terminal-x7k9q2w1.host-0af1b2c3d4e5f60718293a4b5c6d7e8f.localhost/",
    );
  });

  it("applies the same prefix rule on a longer shared base hostname", () => {
    expect(
      deriveServiceOrigin(
        "terminal-x7k9q2w1",
        "host-0af1b2c3d4e5f60718293a4b5c6d7e8f.user.us-east.imbueminds.com",
        "https:",
      ),
    ).toBe("https://terminal-x7k9q2w1.host-0af1b2c3d4e5f60718293a4b5c6d7e8f.user.us-east.imbueminds.com/");
  });

  it("prefixes whatever hostname label it is handed as an ordinary label", () => {
    // deriveServiceOrigin is a pure function of the label; a bare name (used as
    // a fallback when a service has no minted label) nests identically.
    expect(deriveServiceOrigin("my-service", "host-0af1b2c3d4e5f60718293a4b5c6d7e8f.localhost:8421", "http:")).toBe(
      "http://my-service.host-0af1b2c3d4e5f60718293a4b5c6d7e8f.localhost:8421/",
    );
  });
});

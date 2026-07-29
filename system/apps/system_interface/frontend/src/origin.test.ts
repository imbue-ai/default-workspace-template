import { describe, expect, it } from "vitest";

import { deriveServiceOrigin } from "./origin";

describe("deriveServiceOrigin", () => {
  it("nests the service as a hostname label on a local workspace host", () => {
    expect(deriveServiceOrigin("terminal", "host-0af1b2c3d4e5f60718293a4b5c6d7e8f.localhost:8421", "http:")).toBe(
      "http://terminal.host-0af1b2c3d4e5f60718293a4b5c6d7e8f.localhost:8421/",
    );
  });

  it("handles a local workspace host without a port", () => {
    expect(deriveServiceOrigin("terminal", "host-0af1b2c3d4e5f60718293a4b5c6d7e8f.localhost", "http:")).toBe(
      "http://terminal.host-0af1b2c3d4e5f60718293a4b5c6d7e8f.localhost/",
    );
  });

  it("applies the same prefix rule on a longer shared base hostname", () => {
    expect(
      deriveServiceOrigin("terminal", "host-0af1b2c3d4e5f60718293a4b5c6d7e8f.user.us-east.imbueminds.com", "https:"),
    ).toBe("https://terminal.host-0af1b2c3d4e5f60718293a4b5c6d7e8f.user.us-east.imbueminds.com/");
  });

  it("keeps hyphenated service names as ordinary labels", () => {
    expect(deriveServiceOrigin("my-service", "host-0af1b2c3d4e5f60718293a4b5c6d7e8f.localhost:8421", "http:")).toBe(
      "http://my-service.host-0af1b2c3d4e5f60718293a4b5c6d7e8f.localhost:8421/",
    );
  });
});

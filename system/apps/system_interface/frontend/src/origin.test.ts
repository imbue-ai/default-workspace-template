import { describe, expect, it } from "vitest";

import { deriveServiceOrigin, workspaceHostCoordinate } from "./origin";

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

  it("strips the shell's leading label so a service origin never nests under the shell", () => {
    // The shell runs at its OWN label origin (the bare origin redirects there
    // locally; only *.<domain> is served on a share). Deriving relative to
    // that host verbatim would produce terminal-*.system_interface-*.host-<hex>,
    // which routes back to the shell -- a dockview inside a dockview.
    expect(
      deriveServiceOrigin(
        "terminal-x7k9q2w1",
        "system_interface-729saevh.host-0af1b2c3d4e5f60718293a4b5c6d7e8f.localhost:8421",
        "http:",
      ),
    ).toBe("http://terminal-x7k9q2w1.host-0af1b2c3d4e5f60718293a4b5c6d7e8f.localhost:8421/");
  });

  it("strips the shell's leading label on a shared base hostname too", () => {
    expect(
      deriveServiceOrigin(
        "terminal-x7k9q2w1",
        "system_interface-729saevh.host-0af1b2c3d4e5f60718293a4b5c6d7e8f.user.us-east.imbueminds.com",
        "https:",
      ),
    ).toBe("https://terminal-x7k9q2w1.host-0af1b2c3d4e5f60718293a4b5c6d7e8f.user.us-east.imbueminds.com/");
  });
});

describe("workspaceHostCoordinate", () => {
  it("returns the coordinate unchanged when the host has no leading service label", () => {
    expect(workspaceHostCoordinate("host-0af1b2c3d4e5f60718293a4b5c6d7e8f.localhost:8421")).toBe(
      "host-0af1b2c3d4e5f60718293a4b5c6d7e8f.localhost:8421",
    );
  });

  it("strips one or more leading service labels back to the coordinate", () => {
    expect(
      workspaceHostCoordinate("terminal-x7k9q2w1.host-0af1b2c3d4e5f60718293a4b5c6d7e8f.localhost:8421"),
    ).toBe("host-0af1b2c3d4e5f60718293a4b5c6d7e8f.localhost:8421");
    expect(workspaceHostCoordinate("a.b.host-0af1b2c3d4e5f60718293a4b5c6d7e8f.user.region.example.com")).toBe(
      "host-0af1b2c3d4e5f60718293a4b5c6d7e8f.user.region.example.com",
    );
  });

  it("leaves a non-workspace host untouched", () => {
    expect(workspaceHostCoordinate("example.com:8443")).toBe("example.com:8443");
  });
});

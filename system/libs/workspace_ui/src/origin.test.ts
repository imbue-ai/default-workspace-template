import { describe, expect, it } from "vitest";

import { deriveAppOrigin, workspaceHostCoordinate } from "./origin";

describe("deriveAppOrigin", () => {
  it("nests the app's origin label as a hostname label on a local workspace host", () => {
    expect(
      deriveAppOrigin("terminal-x7k9q2w1", "host-0af1b2c3d4e5f60718293a4b5c6d7e8f.localhost:8421", "http:"),
    ).toBe("http://terminal-x7k9q2w1.host-0af1b2c3d4e5f60718293a4b5c6d7e8f.localhost:8421/");
  });

  it("handles a local workspace host without a port", () => {
    expect(deriveAppOrigin("terminal-x7k9q2w1", "host-0af1b2c3d4e5f60718293a4b5c6d7e8f.localhost", "http:")).toBe(
      "http://terminal-x7k9q2w1.host-0af1b2c3d4e5f60718293a4b5c6d7e8f.localhost/",
    );
  });

  it("applies the same prefix rule on a longer shared base hostname", () => {
    expect(
      deriveAppOrigin(
        "terminal-x7k9q2w1",
        "host-0af1b2c3d4e5f60718293a4b5c6d7e8f.user.us-east.imbueminds.com",
        "https:",
      ),
    ).toBe("https://terminal-x7k9q2w1.host-0af1b2c3d4e5f60718293a4b5c6d7e8f.user.us-east.imbueminds.com/");
  });

  it("prefixes whatever hostname label it is handed as an ordinary label", () => {
    // deriveAppOrigin is a pure function of the label; a bare name (used as
    // a fallback when an app has no minted label) nests identically.
    expect(deriveAppOrigin("my-service", "host-0af1b2c3d4e5f60718293a4b5c6d7e8f.localhost:8421", "http:")).toBe(
      "http://my-service.host-0af1b2c3d4e5f60718293a4b5c6d7e8f.localhost:8421/",
    );
  });

  it("strips the shell's leading label so an app origin never nests under the shell", () => {
    // The shell runs at its OWN label origin (the bare origin redirects there
    // locally; only *.<domain> is served on a share). Deriving relative to
    // that host verbatim would produce terminal-*.system_interface-*.host-<hex>,
    // which routes back to the shell -- a dockview inside a dockview.
    expect(
      deriveAppOrigin(
        "terminal-x7k9q2w1",
        "system_interface-729saevh.host-0af1b2c3d4e5f60718293a4b5c6d7e8f.localhost:8421",
        "http:",
      ),
    ).toBe("http://terminal-x7k9q2w1.host-0af1b2c3d4e5f60718293a4b5c6d7e8f.localhost:8421/");
  });

  it("strips the shell's leading label on a shared base hostname too", () => {
    expect(
      deriveAppOrigin(
        "terminal-x7k9q2w1",
        "system_interface-729saevh.host-0af1b2c3d4e5f60718293a4b5c6d7e8f.user.us-east.imbueminds.com",
        "https:",
      ),
    ).toBe("https://terminal-x7k9q2w1.host-0af1b2c3d4e5f60718293a4b5c6d7e8f.user.us-east.imbueminds.com/");
  });

  it("strips the shell's leading label on a workspace-keyed share hostname", () => {
    // The workspace-keyed share-domain shape has no host-<hex> label: its
    // coordinate leads with the bare 32-hex share label. Deriving relative to
    // the shell's host verbatim would nest the app under the shell's own
    // label -- a hostname the relay has no tunnel claim for, so every panel
    // died with an unrecognized_name TLS alert.
    expect(
      deriveAppOrigin(
        "terminal-x7k9q2w1",
        "system_interface-729saevh.5f13881abca599b0e91695294922fd15.103de49d5bad06cb6892f8c9e68c0cf6.us1.imbueminds.com",
        "https:",
      ),
    ).toBe(
      "https://terminal-x7k9q2w1.5f13881abca599b0e91695294922fd15.103de49d5bad06cb6892f8c9e68c0cf6.us1.imbueminds.com/",
    );
  });
});

describe("workspaceHostCoordinate", () => {
  it("returns the coordinate unchanged when the host has no leading app label", () => {
    expect(workspaceHostCoordinate("host-0af1b2c3d4e5f60718293a4b5c6d7e8f.localhost:8421")).toBe(
      "host-0af1b2c3d4e5f60718293a4b5c6d7e8f.localhost:8421",
    );
  });

  it("strips one or more leading app labels back to the coordinate", () => {
    expect(workspaceHostCoordinate("terminal-x7k9q2w1.host-0af1b2c3d4e5f60718293a4b5c6d7e8f.localhost:8421")).toBe(
      "host-0af1b2c3d4e5f60718293a4b5c6d7e8f.localhost:8421",
    );
    expect(workspaceHostCoordinate("a.b.host-0af1b2c3d4e5f60718293a4b5c6d7e8f.user.region.example.com")).toBe(
      "host-0af1b2c3d4e5f60718293a4b5c6d7e8f.user.region.example.com",
    );
  });

  it("strips leading app labels back to a workspace-keyed share coordinate", () => {
    expect(
      workspaceHostCoordinate(
        "system_interface-729saevh.5f13881abca599b0e91695294922fd15.103de49d5bad06cb6892f8c9e68c0cf6.us1.imbueminds.com",
      ),
    ).toBe("5f13881abca599b0e91695294922fd15.103de49d5bad06cb6892f8c9e68c0cf6.us1.imbueminds.com");
  });

  it("does not mistake an app label for a coordinate label", () => {
    // Minted labels are always <name>-<rand>: the hyphen and non-hex name keep
    // them out of both the host-/agent- and the bare-32-hex coordinate shapes.
    expect(workspaceHostCoordinate("files-t1gi0k13.5f13881abca599b0e91695294922fd15.user.us1.imbueminds.com")).toBe(
      "5f13881abca599b0e91695294922fd15.user.us1.imbueminds.com",
    );
  });

  it("leaves a non-workspace host untouched", () => {
    expect(workspaceHostCoordinate("example.com:8443")).toBe("example.com:8443");
  });
});

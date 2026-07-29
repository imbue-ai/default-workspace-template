import { describe, expect, it } from "vitest";

import { deriveServiceOrigin } from "./origin";

describe("deriveServiceOrigin", () => {
  it("nests the service as a subdomain label on a local workspace host", () => {
    expect(deriveServiceOrigin("terminal", "agent-abc123.localhost:8421", "http:")).toBe(
      "http://terminal.agent-abc123.localhost:8421/",
    );
  });

  it("handles a local workspace host without a port", () => {
    expect(deriveServiceOrigin("terminal", "agent-abc123.localhost", "http:")).toBe(
      "http://terminal.agent-abc123.localhost/",
    );
  });

  it("handles the 127.0.0.1 spelling of a local workspace host", () => {
    expect(deriveServiceOrigin("terminal", "agent-abc123.127.0.0.1:8421", "http:")).toBe(
      "http://terminal.agent-abc123.127.0.0.1:8421/",
    );
  });

  it("swaps the first -- token on a shared host", () => {
    expect(deriveServiceOrigin("terminal", "system_interface--myhost--amir.imbue.app", "https:")).toBe(
      "https://terminal--myhost--amir.imbue.app/",
    );
  });

  it("keeps single hyphens in service names distinct from the -- separator", () => {
    // Locally the hyphenated name is just another subdomain label...
    expect(deriveServiceOrigin("my-service", "agent-abc123.localhost:8421", "http:")).toBe(
      "http://my-service.agent-abc123.localhost:8421/",
    );
    // ...and on a shared host it replaces the leading token wholesale, even
    // when that token itself contains single hyphens (``system_interface``).
    expect(deriveServiceOrigin("my-service", "system_interface--myhost--amir.imbue.app", "https:")).toBe(
      "https://my-service--myhost--amir.imbue.app/",
    );
  });
});

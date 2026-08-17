import { describe, expect, it } from "vitest";
import { BUNDLED_MARK_COUNT, serviceMarkUrl } from "./service-marks";

describe("serviceMarkUrl", () => {
  it("bundles the minds service marks", () => {
    // A glob that stops matching (a moved vendor tree) yields an empty map, and
    // every card would quietly show the cube instead of anything failing here.
    expect(BUNDLED_MARK_COUNT).toBeGreaterThan(20);
  });

  it("resolves a scope to its service's mark", () => {
    expect(serviceMarkUrl("slack-api")).toContain("slack");
  });

  it("takes the longest service-name prefix, not the shortest", () => {
    // `github-rest-api` is github's REST transport, not a `github-rest` service
    // -- the same walk `candidate_services` does on the backend.
    expect(serviceMarkUrl("github-rest-api")).toContain("github");
    expect(serviceMarkUrl("google-gmail-api")).toContain("google-gmail");
    expect(serviceMarkUrl("notion-mcp-api")).toContain("notion-mcp");
  });

  it("has no mark for a service it ships no artwork for", () => {
    expect(serviceMarkUrl("madeup-api")).toBeNull();
  });

  it("never resolves a scope to a dark-surface variant", () => {
    // This UI has one (light) theme; the `-on-dark` files stay out of the map.
    expect(serviceMarkUrl("aws")).not.toContain("on-dark");
  });
});

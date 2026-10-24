import { describe, expect, it } from "vitest";

import { combinedFilters, inScope, pathOf, routeOf, scopeFilters } from "./routing";

describe("routes", () => {
  it("read the home page, all usage and one item from the path, and write them back", () => {
    for (const path of ["/", "/all", "/p/subscription%3Aanthropic"]) expect(pathOf(routeOf(path))).toBe(path);
    expect(routeOf("/p/spend%3Aopenai")).toEqual({ page: "item", key: "spend:openai" });
    expect(routeOf("/nothing/here")).toEqual({ page: "home" });
  });
});

describe("scopeFilters", () => {
  it("keeps a subscription item to its provider's subscription calls", () => {
    expect(scopeFilters({ page: "item", key: "subscription:anthropic" })).toEqual({
      provider: ["anthropic"],
      billing_mode: ["SUBSCRIPTION"],
    });
  });

  it("keeps a spend item to every other billing mode, unknown included", () => {
    expect(scopeFilters({ page: "item", key: "spend:openai" }).billing_mode).toContain("UNKNOWN");
    expect(scopeFilters({ page: "all" })).toEqual({});
  });
});

describe("combinedFilters", () => {
  it("narrows the item's values by the page's filter, and lets a filter outside them match nothing", () => {
    const scope = scopeFilters({ page: "item", key: "spend:anthropic" });

    expect(combinedFilters(scope, { billing_mode: "API_KEY", model: "m" })).toMatchObject({
      provider: ["anthropic"],
      billing_mode: ["API_KEY"],
      model: ["m"],
    });
    expect(combinedFilters(scope, { billing_mode: "SUBSCRIPTION" }).billing_mode).toEqual([]);
  });
});

describe("inScope", () => {
  it("takes a streamed call only on its own item's page, and every call on All", () => {
    const call = { provider: "anthropic", billing_mode: "SUBSCRIPTION" };

    expect(inScope(call, { page: "item", key: "subscription:anthropic" })).toBe(true);
    expect(inScope(call, { page: "item", key: "spend:anthropic" })).toBe(false);
    expect(inScope(call, { page: "all" })).toBe(true);
  });
});

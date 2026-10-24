import { describe, expect, it } from "vitest";

import { totalsRow } from "../testing/rows";
import { detailSections, inSection, sectionKeyOf } from "./sections";

describe("detailSections", () => {
  it("splits a provider's subscription and pay-as-you-go use, subscriptions first", () => {
    const sections = detailSections([
      totalsRow({ group: { provider: "anthropic", billing_mode: "API_KEY" }, calls: 9 }),
      totalsRow({ group: { provider: "anthropic", billing_mode: "UNKNOWN" }, calls: 2 }),
      totalsRow({ group: { provider: "anthropic", billing_mode: "SUBSCRIPTION" }, calls: 1 }),
      totalsRow({ group: { provider: "openai", billing_mode: "SUBSCRIPTION" }, calls: 5 }),
    ]);

    expect(sections.map((section) => [section.title, section.billingModes])).toEqual([
      ["ChatGPT subscription (Codex)", ["SUBSCRIPTION"]],
      ["Claude subscription", ["SUBSCRIPTION"]],
      ["Anthropic API", ["API_KEY", "UNKNOWN"]],
    ]);
  });

  it("leaves out groups with no calls", () => {
    expect(detailSections([totalsRow({ group: { provider: "google", billing_mode: "API_KEY" }, calls: 0 })])).toEqual(
      [],
    );
  });
});

describe("inSection", () => {
  it("puts every non-subscription mode of a provider in its one spend section", () => {
    const [section] = detailSections([totalsRow({ group: { provider: "openai", billing_mode: "API_KEY" } })]);

    expect(inSection({ provider: "openai", billing_mode: "UNKNOWN" }, section)).toBe(true);
    expect(inSection({ provider: "openai", billing_mode: "SUBSCRIPTION" }, section)).toBe(false);
    expect(sectionKeyOf({ provider: "openai", billing_mode: "IMBUE_KEY" })).toBe("spend:openai");
  });
});

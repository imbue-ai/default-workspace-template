import { beforeEach, describe, expect, it, vi } from "vitest";
import type { CatalogModelOption } from "./HarnessCatalog";

const mocks = vi.hoisted(() => ({
  request: vi.fn(),
  catalogOptions: [] as CatalogModelOption[],
  accountHarness: "claude" as string | undefined,
  switchMode: "eager_then_reconcile",
}));
vi.mock("mithril", () => ({ default: { request: mocks.request } }));
vi.mock("@imbue/workspace-ui/src/base-path", () => ({ apiUrl: (path: string) => path }));
vi.mock("./HarnessCatalog", () => ({
  ensureHarnessCatalogs: () => Promise.resolve(),
  getHarnessCatalog: (harness: string | undefined) =>
    harness === undefined ? null : { options: mocks.catalogOptions, switch_mode: mocks.switchMode },
}));
vi.mock("./Providers", () => ({
  accountForAgent: () => (mocks.accountHarness === undefined ? null : { harness: mocks.accountHarness }),
}));

import { fetchAccountModelOptions } from "./AccountModelOptions";

function option(id: string, isInPicker: boolean): CatalogModelOption {
  return {
    id,
    label: id.toUpperCase(),
    efforts: [],
    supports_fast: false,
    in_picker: isInPicker,
    harness_reported_model_id: null,
  } as CatalogModelOption;
}

describe("fetchAccountModelOptions", () => {
  beforeEach(() => {
    mocks.request.mockReset();
    mocks.accountHarness = "claude";
    mocks.switchMode = "eager_then_reconcile";
    mocks.catalogOptions = [option("sonnet", true), option("internal", false)];
  });

  it("offers the options the route names for a harness whose set is per account", async () => {
    mocks.request.mockResolvedValue({ options: [option("gpt-6-astra", true), option("gpt-6-lab", false)] });
    // Not the catalog: codex's set is the account's own, and the picker-hidden entry is dropped.
    expect((await fetchAccountModelOptions("acct-openai")).map((each) => each.id)).toEqual(["gpt-6-astra"]);
  });

  it("offers nothing for an account whose harness names an empty set", async () => {
    mocks.request.mockResolvedValue({ options: [] });
    expect(await fetchAccountModelOptions("acct-openai")).toEqual([]);
  });

  it("falls back to the harness's catalog when the route names no options", async () => {
    mocks.request.mockResolvedValue({ options: null });
    expect((await fetchAccountModelOptions("acct-anthropic")).map((each) => each.id)).toEqual(["sonnet"]);
  });

  it("offers nothing for a harness whose model the chat app cannot switch, whatever its catalog lists", async () => {
    // agy's model is changed from the agent's terminal, so a pick armed here could never be applied.
    mocks.request.mockResolvedValue({ options: null });
    mocks.accountHarness = "antigravity";
    mocks.switchMode = "read_only";
    expect(await fetchAccountModelOptions("acct-google")).toEqual([]);
  });

  it("offers nothing for an account on a harness this build has no catalog for", async () => {
    mocks.request.mockResolvedValue({ options: null });
    mocks.accountHarness = undefined;
    expect(await fetchAccountModelOptions("acct-unknown")).toEqual([]);
  });
});

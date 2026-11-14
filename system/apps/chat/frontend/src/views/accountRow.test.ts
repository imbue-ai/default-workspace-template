// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import type m from "mithril";
import type { ProviderAccount } from "../models/Providers";

vi.mock("../models/Providers", () => ({
  deleteAccount: vi.fn(),
  loadAccounts: vi.fn(),
  renameAccount: vi.fn(),
  setDefaultAccount: vi.fn(),
}));

import { accountRow, emptyAccountRowState, type AccountRowState } from "./accountRow";

const ACCOUNT: ProviderAccount = {
  id: "acct-1",
  lane: "claude",
  harness: "claude",
  provider: "Anthropic",
  harness_label: "Claude Code",
  seq: 1,
  name: "",
  label: "Anthropic (Claude Code)",
  reauth_method: "browser",
  signed_in_at: null,
};

function renameKeydown(state: AccountRowState): (event: unknown) => void {
  const wrap = accountRow({
    row: ACCOUNT,
    isCurrent: false,
    isDefault: false,
    rowClass: "",
    onSelect: () => undefined,
    state,
  });
  const [input] = wrap.children as m.Vnode[];
  return (input.attrs as { onkeydown: (event: unknown) => void }).onkeydown;
}

describe("accountRow rename during an input-method composition", () => {
  it("leaves Enter to the input method, and commits on the Enter that follows", () => {
    const state: AccountRowState = { ...emptyAccountRowState(), renamingId: ACCOUNT.id };
    const onkeydown = renameKeydown(state);
    const blur = vi.fn();

    onkeydown({ key: "Enter", isComposing: true, target: { blur } });
    onkeydown({ key: "Enter", keyCode: 229, target: { blur } });
    expect(blur).not.toHaveBeenCalled();

    onkeydown({ key: "Enter", target: { blur } });
    expect(blur).toHaveBeenCalledTimes(1);
  });

  it("keeps the field open when Escape only cancels a composition", () => {
    const state: AccountRowState = { ...emptyAccountRowState(), renamingId: ACCOUNT.id };
    const onkeydown = renameKeydown(state);

    onkeydown({ key: "Escape", isComposing: true, target: { blur: vi.fn() } });
    expect(state.renamingId).toBe(ACCOUNT.id);

    onkeydown({ key: "Escape", target: { blur: vi.fn() } });
    expect(state.renamingId).toBeNull();
  });
});

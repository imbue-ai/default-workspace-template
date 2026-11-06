// @vitest-environment jsdom
/** The early sign-in page: the chooser on its own, which tells the program when the user is done with it. */
import { afterEach, describe, expect, it, vi } from "vitest";
import m from "mithril";

import { closeProviderChooser, isProviderChooserOpen, openProviderChooser } from "../models/Providers";
import { ProviderChooserModal } from "../views/ProviderChooserModal";
import { SignInPage } from "./index";

afterEach(() => {
  closeProviderChooser();
  vi.restoreAllMocks();
});

describe("SignInPage", () => {
  it("shows the chooser, and dismissing it closes it and reports the user done", () => {
    const request = vi.spyOn(m, "request").mockResolvedValue(undefined);
    openProviderChooser();

    const shown = SignInPage.view(m(SignInPage) as never) as m.Vnode<{ onDismiss: () => void }>;
    expect(shown.tag).toBe(ProviderChooserModal);
    shown.attrs.onDismiss();

    expect(isProviderChooserOpen()).toBe(false);
    expect(request).toHaveBeenCalledWith({ method: "POST", url: "/api/early-sign-in/done" });
    expect(SignInPage.view(m(SignInPage) as never)).toBeNull();
  });
});

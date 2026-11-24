// @vitest-environment jsdom
import "../testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { KeybindingAppView, KeybindingsCatalog, KeybindingsDeliveryOutcome } from "../model/api";
import { wordFailureAsTheStoreDoes } from "../testing/failures";
import { settle } from "../testing/fakeShell";
import { KeyboardSettingsSection, chordsLabel, conflictingAction } from "./KeyboardSettingsSection";

const APP: KeybindingAppView = {
  app: "files",
  display_name: "Files",
  actions: [
    {
      id: "rename",
      label: "Rename the selected entry",
      default_chords: ["f2"],
      chords: ["f2"],
      source: "default",
    },
    {
      id: "delete",
      label: "Delete the selection",
      default_chords: ["delete"],
      chords: ["delete", "ctrl+backspace"],
      source: "workspace",
    },
  ],
};

describe("chordsLabel", () => {
  it("joins the chords and names an unbound action", () => {
    expect(chordsLabel(["ctrl+a", "meta+a"])).toBe("ctrl+a, meta+a");
    expect(chordsLabel([])).toBe("not bound");
  });
});

describe("conflictingAction", () => {
  it("finds another action of the same app holding the chord, and never the action itself", () => {
    expect(conflictingAction(APP, "rename", "ctrl+backspace")?.id).toBe("delete");
    expect(conflictingAction(APP, "delete", "ctrl+backspace")).toBeNull();
    expect(conflictingAction(APP, "rename", "ctrl+n")).toBeNull();
  });
});

/** The owner's catalog with one "everywhere" save the account has not taken yet. */
const CATALOG_WITH_UNSENT: KeybindingsCatalog = {
  apps: [APP],
  is_owner: true,
  can_save_everywhere: true,
  has_pending_permission: false,
  unsent: [{ app: "files", action: "rename", chords: ["ctrl+r"] }],
};

/** A fetch answering the catalog read and the deliver route the way the shell does. */
function stubFetch(delivery: KeybindingsDeliveryOutcome): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => ({
      ok: true,
      status: 200,
      json: async () => (url.endsWith("/api/keybindings/deliver") ? delivery : CATALOG_WITH_UNSENT),
    })),
  );
}

/** Mount the section and let its catalog read land. */
async function mountSection(): Promise<HTMLElement> {
  const root = mountView(() =>
    m(KeyboardSettingsSection, { wordFailure: wordFailureAsTheStoreDoes, onReportBug: null }),
  );
  await settle();
  m.redraw.sync();
  return root;
}

function retryButton(root: HTMLElement): HTMLButtonElement {
  const buttons = Array.from(root.querySelectorAll("[data-keybinding-unsent] button"));
  return buttons.find((button) => button.textContent === "Try saving to your account again") as HTMLButtonElement;
}

describe("the unsent saves' retry", () => {
  afterEach(() => {
    unmountViews();
    vi.unstubAllGlobals();
  });

  it("shows why a retry left the saves unsent", async () => {
    stubFetch({ status: "UNREACHABLE", detail: "Imbue Studio could not be reached; the change stays here." });
    const root = await mountSection();

    retryButton(root).click();
    await settle();
    m.redraw.sync();

    expect(root.textContent).toContain("Imbue Studio could not be reached; the change stays here.");
  });

  it("shows nothing once a retry delivered them", async () => {
    stubFetch({ status: "DELIVERED", detail: "" });
    const root = await mountSection();

    retryButton(root).click();
    await settle();
    m.redraw.sync();

    expect(root.textContent).not.toContain("Imbue Studio");
    expect(root.querySelector("[data-keybinding-unsent]")).not.toBeNull();
  });
});

/** A fetch answering each call from ``answers`` in turn: a status and the JSON body the shell sends. */
function stubFetchAnswers(answers: readonly { status: number; body: unknown }[]): ReturnType<typeof vi.fn> {
  const queue = [...answers];
  const fetchMock = vi.fn(async () => {
    const answer = queue.shift() as { status: number; body: unknown };
    return new Response(JSON.stringify(answer.body), { status: answer.status });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("the section's failures", () => {
  afterEach(() => {
    unmountViews();
    vi.unstubAllGlobals();
  });

  it("says the shortcuts couldn't be loaded, with the shell's reason behind See details, and reads them again on Try again", async () => {
    stubFetchAnswers([
      { status: 500, body: { detail: "the registry could not be read" } },
      { status: 200, body: CATALOG_WITH_UNSENT },
    ]);
    const root = await mountSection();

    expect(root.querySelector(".keyboard-settings-failure-message")?.textContent).toBe(
      "Your keyboard shortcuts couldn't be loaded. Try again.",
    );
    expect(root.querySelector(".see-details pre")?.textContent).toBe("HTTP 500\nthe registry could not be read");

    (root.querySelector(".keyboard-settings-failure-action") as HTMLButtonElement).click();
    await settle();
    m.redraw.sync();

    expect(root.querySelector(".keyboard-settings-failure")).toBeNull();
    expect(root.querySelector('[data-keybinding-action="files.rename"]')).not.toBeNull();
  });

  it("says which change failed, keeping the shortcuts listed, with the shell's refusal behind See details", async () => {
    stubFetchAnswers([
      { status: 200, body: CATALOG_WITH_UNSENT },
      { status: 400, body: { detail: "no override of files.delete to reset" } },
    ]);
    const root = await mountSection();
    const reset = Array.from(root.querySelectorAll('[data-keybinding-action="files.delete"] button')).find(
      (button) => button.textContent === "Reset",
    ) as HTMLButtonElement;

    reset.click();
    await settle();
    m.redraw.sync();

    expect(root.querySelector(".keyboard-settings-failure-message")?.textContent).toBe(
      "This shortcut couldn't be reset.",
    );
    expect(root.querySelector(".see-details pre")?.textContent).toBe("HTTP 400\nno override of files.delete to reset");
    expect(root.querySelector('[data-keybinding-action="files.rename"]')).not.toBeNull();
  });
});

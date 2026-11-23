// @vitest-environment jsdom
import "@imbue/workspace-ui/src/testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import m from "mithril";
import { afterEach, describe, expect, it } from "vitest";
import type { ChecklistItem } from "../models/ChecklistItems";
import { ChecklistPage } from "./ChecklistPage";

afterEach(() => {
  unmountViews();
});

const ITEMS: readonly ChecklistItem[] = [
  { id: "new_chat", label: "Work in parallel by starting a new chat", isDone: true },
  { id: "phone_access", label: "Access Studio from your phone", isDone: false },
  { id: "build_app", label: "Build an app", isDone: true },
  { id: "connect_data", label: "Connect some data", isDone: false },
];

function render(
  items: readonly ChecklistItem[] | null,
  showsCoachMarks = true,
): { root: HTMLElement; picked: string[] } {
  const picked: string[] = [];
  const root = mountView(() => m(ChecklistPage, { items, showsCoachMarks, onPick: (itemId) => picked.push(itemId) }));
  return { root, picked };
}

function rows(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>("[data-checklist-item]"));
}

describe("the Onboarding checklist page", () => {
  it("is titled Onboarding checklist and lists the items in order with their words", () => {
    const { root } = render(ITEMS);

    expect(root.querySelector("[data-checklist-title]")?.textContent).toBe("Onboarding checklist");
    expect(rows(root).map((row) => [row.dataset.checklistItem, row.textContent])).toEqual([
      ["new_chat", "Work in parallel by starting a new chat"],
      ["phone_access", "Access Studio from your phone"],
      ["build_app", "Build an app"],
      ["connect_data", "Connect some data"],
    ]);
  });

  it("says nothing but its title and the items' words", () => {
    const { root } = render(ITEMS);

    expect(root.textContent).toBe(`Onboarding checklist${ITEMS.map((item) => item.label).join("")}`);
  });

  it("draws a check on the done items and an empty mark on the others", () => {
    const { root } = render(ITEMS);

    expect(rows(root).map((row) => row.dataset.done)).toEqual(["true", "false", "true", "false"]);
    expect(rows(root).map((row) => row.querySelector("svg") !== null)).toEqual([true, false, true, false]);
  });

  it("shows an item's pointer on a click of its row, done or not", () => {
    const { root, picked } = render(ITEMS);

    for (const row of rows(root)) row.click();

    expect(rows(root).map((row) => row.tagName)).toEqual(["BUTTON", "BUTTON", "BUTTON", "BUTTON"]);
    expect(picked).toEqual(["new_chat", "phone_access", "build_app", "connect_data"]);
  });

  it("lists phone access and connecting data as plain rows where no chrome draws coach marks", () => {
    const { root, picked } = render(ITEMS, false);

    for (const row of rows(root)) row.click();

    expect(rows(root).map((row) => row.tagName)).toEqual(["BUTTON", "DIV", "BUTTON", "DIV"]);
    expect(picked).toEqual(["new_chat", "build_app"]);
  });

  it("shows its title alone until the items have been read", () => {
    const { root } = render(null);

    expect(root.querySelector("[data-checklist-title]")).not.toBeNull();
    expect(rows(root)).toEqual([]);
  });
});

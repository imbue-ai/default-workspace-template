// @vitest-environment jsdom
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { beforeEach, describe, expect, it, vi } from "vitest";
import m from "mithril";

const mocks = vi.hoisted(() => ({ showPickerSendHintOnce: vi.fn() }));

vi.mock("./PickerSendHint", () => ({ showPickerSendHintOnce: mocks.showPickerSendHintOnce }));
// A build whose bundled artwork holds no mark for three of the connectors.
vi.mock("./service-marks", () => ({
  serviceMarkUrl: (service: string) =>
    ["instagram", "youtube", "hubspot"].includes(service) ? null : `/assets/${service}.svg`,
}));

import { readComposerDraft } from "../models/ComposerDraft";
import {
  CATEGORY_PICKER_OTHER_PROMPT,
  CONNECTOR_PICKER_OTHER_PROMPT,
  PICKER_CATEGORY_COLUMNS,
  PICKER_CONNECTORS,
  PICKER_OTHER_LABEL,
  StudioPicker,
} from "./StudioPickers";

const CHAT = "agent-first-run";
let root: HTMLElement;

function draw(kind: "category_picker" | "connector_picker", isLive = true): void {
  m.render(root, m(StudioPicker, { kind, chatId: CHAT, isLive }));
}

function pills(): HTMLButtonElement[] {
  return [...root.querySelectorAll<HTMLButtonElement>(".studio-picker-pill")];
}

function pill(label: string): HTMLButtonElement {
  const found = pills().find((each) => each.textContent === label);
  if (found === undefined) throw new Error(`no pill labelled ${label}`);
  return found;
}

function pressedLabels(): string[] {
  return pills()
    .filter((each) => each.getAttribute("aria-pressed") === "true")
    .map((each) => each.textContent ?? "");
}

beforeEach(() => {
  localStorage.clear();
  root = document.createElement("div");
  mocks.showPickerSendHintOnce.mockClear();
  vi.spyOn(m, "redraw").mockImplementation(() => undefined);
});

describe("the category picker", () => {
  it("draws the twenty-three categories in three titled columns, and Other at the end of the last", () => {
    draw("category_picker");

    const columns = [...root.querySelectorAll(".studio-picker-column")];
    expect(columns.map((column) => column.querySelector(".studio-picker-heading")?.textContent)).toEqual([
      "Work",
      "Life",
      "Creative",
    ]);
    expect(columns.map((column) => column.querySelectorAll(".studio-picker-pill").length)).toEqual([8, 8, 8]);
    expect(PICKER_CATEGORY_COLUMNS.flatMap((column) => column.categories)).toHaveLength(23);
    expect([...columns[2].querySelectorAll(".studio-picker-pill")].map((each) => each.textContent)).toEqual([
      "Writing",
      "Art & design",
      "Film & video",
      "Game dev",
      "Books",
      "Movies",
      "Music",
      PICKER_OTHER_LABEL,
    ]);
    expect(pill("Music").disabled).toBe(false);
  });

  it("puts a prompt to write over in the draft for Other, which toggles and reads as pressed like any pill", () => {
    draw("category_picker");
    pill("Money").click();
    draw("category_picker");
    pill(PICKER_OTHER_LABEL).click();
    draw("category_picker");

    expect(readComposerDraft(CHAT)).toBe(`Money, ${CATEGORY_PICKER_OTHER_PROMPT}, `);
    expect(pressedLabels()).toEqual(["Money", PICKER_OTHER_LABEL]);
    expect(mocks.showPickerSendHintOnce).toHaveBeenCalledTimes(2);

    pill(PICKER_OTHER_LABEL).click();
    draw("category_picker");

    expect(readComposerDraft(CHAT)).toBe("Money, ");
    expect(pressedLabels()).toEqual(["Money"]);
  });

  it("does not read Other as pressed from a draft that only says Other", () => {
    localStorage.setItem(`message-text:${CHAT}`, `${PICKER_OTHER_LABEL}, `);

    draw("category_picker");

    expect(pressedLabels()).toEqual([]);
  });

  it("puts each category clicked into the draft in the order clicked, and takes one out on a second click", () => {
    draw("category_picker");
    pill("Money").click();
    draw("category_picker");
    pill("Sales").click();
    draw("category_picker");
    expect(readComposerDraft(CHAT)).toBe("Money, Sales, ");
    expect(pressedLabels()).toEqual(["Sales", "Money"]);

    pill("Money").click();
    draw("category_picker");

    expect(readComposerDraft(CHAT)).toBe("Sales, ");
    expect(pressedLabels()).toEqual(["Sales"]);
    expect(pill("Sales").className).toContain("studio-picker-pill--pressed");
  });

  it("reads a pill as pressed from the draft alone, so it comes back pressed after a reload", () => {
    localStorage.setItem(`message-text:${CHAT}`, "Health, and my own thing");

    draw("category_picker");

    expect(pressedLabels()).toEqual(["Health"]);
  });

  it("asks for the send hint when a click adds a category, not when it takes one out", () => {
    draw("category_picker");
    pill("Money").click();
    expect(mocks.showPickerSendHintOnce).toHaveBeenCalledTimes(1);

    draw("category_picker");
    pill("Money").click();

    expect(mocks.showPickerSendHintOnce).toHaveBeenCalledTimes(1);
  });

  it("takes no clicks in a message that is no longer the one to answer", () => {
    localStorage.setItem(`message-text:${CHAT}`, "Money, ");
    draw("category_picker", false);

    pill("Sales").click();

    expect(pills().every((each) => each.disabled)).toBe(true);
    expect(readComposerDraft(CHAT)).toBe("Money, ");
    expect(pressedLabels()).toEqual(["Money"]);
  });
});

describe("the connector picker", () => {
  it("draws the fifteen apps in order, each with its mark where one is bundled and by name alone otherwise, then Other", () => {
    draw("connector_picker");

    expect(pills().map((each) => each.textContent)).toEqual([
      ...PICKER_CONNECTORS.map((connector) => connector.name),
      PICKER_OTHER_LABEL,
    ]);
    expect(PICKER_CONNECTORS).toHaveLength(15);
    expect(pill(PICKER_OTHER_LABEL).querySelector("img")).toBeNull();
    expect(pill("Gmail").querySelector("img")?.getAttribute("src")).toBe("/assets/google-gmail.svg");
    expect(pill("Notion").querySelector("img")?.getAttribute("src")).toBe("/assets/notion-mcp.svg");
    for (const name of ["Instagram", "YouTube", "HubSpot"]) {
      expect(pill(name).querySelector("img")).toBeNull();
    }
  });

  it("puts an app into the draft and never asks for the send hint", () => {
    draw("connector_picker");
    pill("Gmail").click();
    draw("connector_picker");
    pill("Google Calendar").click();
    draw("connector_picker");

    expect(readComposerDraft(CHAT)).toBe("Gmail, Google Calendar, ");
    expect(pressedLabels()).toEqual(["Gmail", "Google Calendar"]);
    expect(mocks.showPickerSendHintOnce).not.toHaveBeenCalled();
  });

  it("puts its own prompt in the draft for Other, and never asks for the send hint", () => {
    draw("connector_picker");
    pill("Gmail").click();
    draw("connector_picker");
    pill(PICKER_OTHER_LABEL).click();
    draw("connector_picker");

    expect(readComposerDraft(CHAT)).toBe(`Gmail, ${CONNECTOR_PICKER_OTHER_PROMPT}, `);
    expect(pressedLabels()).toEqual(["Gmail", PICKER_OTHER_LABEL]);
    expect(mocks.showPickerSendHintOnce).not.toHaveBeenCalled();

    pill(PICKER_OTHER_LABEL).click();
    draw("connector_picker");

    expect(readComposerDraft(CHAT)).toBe("Gmail, ");
  });
});

describe("the first-run skill", () => {
  it("tells the agent of every category and app the pickers offer, by the names the pills put in the draft", () => {
    // The agent reads the user's answer against these lists, so a pill the skill does not name would arrive as
    // a category or an app "of their own".
    const skill = readFileSync(
      join(__dirname, "../../../../../../.agents/skills/first-run-onboarding/SKILL.md"),
      "utf8",
    );

    for (const column of PICKER_CATEGORY_COLUMNS) {
      expect(skill).toContain(`- ${column.title}: ${column.categories.join(", ")}\n`);
    }
    const names = PICKER_CONNECTORS.map((connector) => connector.name);
    expect(skill).toContain(`The picker offers ${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}.`);
  });

  it("tells the agent what the Other pills put in a message, and lists Other as no category and no app", () => {
    const skill = readFileSync(
      join(__dirname, "../../../../../../.agents/skills/first-run-onboarding/SKILL.md"),
      "utf8",
    );

    // Either prompt arriving unchanged has to read as nothing picked, so the skill quotes both word for word.
    expect(skill).toContain(`"${CATEGORY_PICKER_OTHER_PROMPT}"`);
    expect(skill).toContain(`"${CONNECTOR_PICKER_OTHER_PROMPT}"`);
    const offered = [
      ...PICKER_CATEGORY_COLUMNS.flatMap((column) => column.categories),
      ...PICKER_CONNECTORS.map((connector) => connector.name),
    ];
    expect(offered).not.toContain(PICKER_OTHER_LABEL);
  });
});

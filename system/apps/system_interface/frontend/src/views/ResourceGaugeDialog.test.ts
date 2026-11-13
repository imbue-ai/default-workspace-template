// @vitest-environment jsdom
import "../testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ResourceGaugeSettings } from "../model/resourceGauge";
import { ResourceGaugeDialog, parseLevelText } from "./ResourceGaugeDialog";

const SETTINGS: ResourceGaugeSettings = {
  visibility: "when_warning",
  diskWarningFreeGb: 5,
  memoryWarningPercent: null,
  cpuWarningPercent: null,
};

afterEach(unmountViews);

function card(): HTMLElement {
  return document.querySelector("[data-resource-gauge-dialog]") as HTMLElement;
}

function levelInput(resource: string): HTMLInputElement {
  return card().querySelector(`[data-gauge-warning-level="${resource}"]`) as HTMLInputElement;
}

function typeInto(input: HTMLInputElement, value: string): void {
  input.value = value;
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

function check(resource: string, isChecked: boolean): void {
  const input = card().querySelector(`[data-gauge-warning-enabled="${resource}"]`) as HTMLInputElement;
  input.checked = isChecked;
  input.dispatchEvent(new Event("change", { bubbles: true }));
  m.redraw.sync();
}

async function save(): Promise<void> {
  m.redraw.sync();
  (card().querySelector(".resource-gauge-save") as HTMLButtonElement).click();
  await new Promise((resolve) => setTimeout(resolve, 0));
  m.redraw.sync();
}

function mount(onSave: (settings: ResourceGaugeSettings) => Promise<void>, onClose: () => void = vi.fn()): void {
  mountView(() => m(ResourceGaugeDialog, { settings: SETTINGS, onSave, onClose }));
}

describe("ResourceGaugeDialog", () => {
  it("saves the visibility and the warning levels the user chose, then closes", async () => {
    const onSave = vi.fn<(settings: ResourceGaugeSettings) => Promise<void>>().mockResolvedValue();
    const onClose = vi.fn();
    mount(onSave, onClose);
    expect(levelInput("disk").value).toBe("5");
    expect(levelInput("memory").disabled).toBe(true);
    (card().querySelector('[data-gauge-visibility="always"]') as HTMLButtonElement).click();
    check("memory", true);
    typeInto(levelInput("memory"), "85");
    typeInto(levelInput("disk"), "2.5");
    await save();
    expect(onSave).toHaveBeenCalledWith({
      visibility: "always",
      diskWarningFreeGb: 2.5,
      memoryWarningPercent: 85,
      cpuWarningPercent: null,
    });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("refuses a level out of range without saving, and turns a warning off", async () => {
    const onSave = vi.fn<(settings: ResourceGaugeSettings) => Promise<void>>().mockResolvedValue();
    mount(onSave);
    typeInto(levelInput("disk"), "0.1");
    await save();
    expect(onSave).not.toHaveBeenCalled();
    expect(card().querySelector("[data-gauge-error]")?.textContent).toContain("0.5 to 500 GB");
    check("disk", false);
    await save();
    expect(onSave).toHaveBeenCalledWith({ ...SETTINGS, diskWarningFreeGb: null });
  });

  it("says why when the shell refuses the save, and stays open", async () => {
    const onSave = vi.fn<(settings: ResourceGaugeSettings) => Promise<void>>().mockRejectedValue(new Error("busy"));
    const onClose = vi.fn();
    mount(onSave, onClose);
    await save();
    expect(card().querySelector("[data-gauge-error]")?.textContent).toBe("Could not save: busy");
    expect(onClose).not.toHaveBeenCalled();
  });

  it("reads a typed level as GB for the disk and a whole percentage for memory and CPU", () => {
    expect(parseLevelText("disk", " 2.5 ")).toBe(2.5);
    expect(parseLevelText("disk", "0.4")).toBeNull();
    expect(parseLevelText("disk", "501")).toBeNull();
    expect(parseLevelText("disk", "five")).toBeNull();
    expect(parseLevelText("memory", "90")).toBe(90);
    expect(parseLevelText("memory", "9.5")).toBeNull();
    expect(parseLevelText("cpu", "49")).toBeNull();
    expect(parseLevelText("cpu", "")).toBeNull();
  });
});

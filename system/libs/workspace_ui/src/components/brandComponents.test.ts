// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import m from "mithril";
import { cardClass } from "./card";
import { Checkbox, Switch, switchVisual } from "./formControls";
import { noticeClass } from "./notice";

function render(vnode: m.Vnode): HTMLElement {
  const root = document.createElement("div");
  m.render(root, vnode);
  return root;
}

describe("cardClass", () => {
  it("is the raised surface with 16px corners, lifting under the pointer only when interactive", () => {
    expect(cardClass().split(" ")).toEqual(
      expect.arrayContaining(["rounded-2xl", "bg-body-surface-raised", "border-default"]),
    );
    expect(cardClass()).not.toContain("hover:");
    expect(cardClass({ interactive: true })).toContain("hover:shadow-raised");
  });

  it("wears the focus colour when chosen, and the overlay surface and floating shadow when it floats", () => {
    expect(cardClass({ selected: true })).toContain("border-focus");
    expect(cardClass({ selected: true })).not.toContain("border-default");
    expect(cardClass({ floating: true }).split(" ")).toEqual(
      expect.arrayContaining(["bg-overlay-surface", "shadow-overlay"]),
    );
  });
});

describe("noticeClass", () => {
  it("tints each status tone with its matching edge and subtle content", () => {
    expect(noticeClass("warn").split(" ")).toEqual(
      expect.arrayContaining(["bg-warn-subtle-surface", "border-warn-4", "text-warn-subtle-content", "rounded-md"]),
    );
    expect(noticeClass("neutral")).toContain("border-default");
    expect(noticeClass("danger", "mx-3")).toContain("mx-3");
  });
});

describe("Checkbox", () => {
  it("is a real checkbox that reports its new state, marked as the checkbox part", () => {
    const onchange = vi.fn();
    const root = render(m(Checkbox, { checked: false, onchange, input: { "data-test": "" } }));
    const input = root.querySelector<HTMLInputElement>("input[type=checkbox][data-test]")!;
    expect(root.querySelector('[data-part="checkbox"]')).not.toBeNull();
    input.checked = true;
    input.dispatchEvent(new Event("change"));
    expect(onchange).toHaveBeenCalledWith(true);
  });
});

describe("Switch", () => {
  it("is a button with the switch role that says whether it is on and toggles on a click", () => {
    const onToggle = vi.fn();
    const root = render(m(Switch, { on: true, label: "Notify me", onToggle }));
    const button = root.querySelector<HTMLButtonElement>('button[role="switch"]')!;
    expect(button.getAttribute("aria-checked")).toBe("true");
    expect(button.textContent).toBe("Notify me");
    button.click();
    expect(onToggle).toHaveBeenCalledTimes(1);
  });

  it("draws the knob at the far end in the primary colour when on, and near the start when off", () => {
    const on = render(switchVisual(true)).querySelector<HTMLElement>('[data-part="switch"]')!;
    const off = render(switchVisual(false)).querySelector<HTMLElement>('[data-part="switch"]')!;
    expect(on.className).toContain("bg-primary-surface");
    expect(on.firstElementChild?.className).toContain("translate-x-4");
    expect(off.className).toContain("bg-neutral-5");
    expect(off.firstElementChild?.className).toContain("translate-x-0.5");
    expect(on.getAttribute("aria-hidden")).toBe("true");
  });
});

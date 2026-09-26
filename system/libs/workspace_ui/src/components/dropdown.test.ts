// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import m from "mithril";

import { Dropdown, DROPDOWN_OPTION_ATTR, DROPDOWN_PART_ATTR } from "./dropdown";

const OPTIONS = [
  { value: "a", label: "Alpha", detail: "A_KEY" },
  { value: "b", label: "Beta" },
] as const;

function part(name: string): HTMLElement | null {
  return document.body.querySelector<HTMLElement>(`[${DROPDOWN_PART_ATTR}="${name}"]`);
}

let root: HTMLDivElement;
const picked: string[] = [];
let value: "a" | "b" | null = null;

function render(): void {
  m.render(
    root,
    m(Dropdown<"a" | "b">, {
      options: OPTIONS,
      value,
      placeholder: "Choose one...",
      onSelect: (next) => {
        picked.push(next);
        value = next;
      },
    }),
  );
}

function click(element: Element | null): void {
  if (element === null) throw new Error("nothing to click");
  element.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  render();
}

beforeEach(() => {
  root = document.createElement("div");
  document.body.appendChild(root);
  picked.length = 0;
  value = null;
  render();
});

afterEach(() => {
  m.render(root, null);
  root.remove();
  document.body.innerHTML = "";
});

describe("Dropdown", () => {
  it("reads its placeholder until something is picked, then the pick and its detail", () => {
    expect(part("trigger")!.textContent).toContain("Choose one...");
    click(part("trigger"));
    click(document.body.querySelector(`[${DROPDOWN_OPTION_ATTR}="a"]`));
    expect(picked).toEqual(["a"]);
    expect(part("trigger")!.textContent).toContain("Alpha");
    expect(part("trigger")!.textContent).toContain("A_KEY");
  });

  it("opens a list of every option under a sheet, and closes on a pick", () => {
    expect(part("list")).toBeNull();
    click(part("trigger"));
    expect(part("list")!.getAttribute("role")).toBe("listbox");
    expect(part("sheet")).not.toBeNull();
    expect(document.body.querySelectorAll(`[${DROPDOWN_OPTION_ATTR}]`)).toHaveLength(2);
    click(document.body.querySelector(`[${DROPDOWN_OPTION_ATTR}="b"]`));
    expect(part("list")).toBeNull();
    expect(part("trigger")!.getAttribute("aria-expanded")).toBe("false");
  });

  it("marks the picked option and no other", () => {
    value = "a";
    render();
    click(part("trigger"));
    const options = document.body.querySelectorAll(`[${DROPDOWN_OPTION_ATTR}]`);
    expect(options[0].getAttribute("aria-selected")).toBe("true");
    expect(options[1].getAttribute("aria-selected")).toBe("false");
  });

  it("closes on a press on the sheet, and on Escape, picking nothing", () => {
    click(part("trigger"));
    part("sheet")!.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
    render();
    expect(part("list")).toBeNull();
    click(part("trigger"));
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    render();
    expect(part("list")).toBeNull();
    expect(picked).toEqual([]);
  });

  it("keeps Escape to itself, so a modal around it does not close too", () => {
    const outer = vi.fn();
    document.addEventListener("keydown", outer);
    click(part("trigger"));
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    expect(outer).not.toHaveBeenCalled();
    document.removeEventListener("keydown", outer);
  });
});

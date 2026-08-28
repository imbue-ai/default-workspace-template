// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import m from "mithril";
import { Button } from "./Button";

/** Render a Button synchronously and return the real <button> element. */
function renderButton(attrs: Record<string, unknown>, label = "Press"): HTMLButtonElement {
  const root = document.createElement("div");
  m.render(root, m(Button, attrs, label));
  const element = root.querySelector("button");
  if (element === null) throw new Error("Button did not render a <button>");
  return element;
}

describe("Button readonly option", () => {
  it("marks the element aria-disabled for assistive tech", () => {
    const element = renderButton({ readonly: true });
    expect(element.getAttribute("aria-disabled")).toBe("true");
  });

  it("lets an explicit caller aria-disabled win over the readonly default", () => {
    const element = renderButton({ readonly: true, "aria-disabled": "false" });
    expect(element.getAttribute("aria-disabled")).toBe("false");
  });

  it("keeps the resting look: default cursor, no aria-disabled dim", () => {
    const element = renderButton({ readonly: true });
    expect(element.classList.contains("cursor-default")).toBe(true);
    expect(element.classList.contains("cursor-pointer")).toBe(false);
    expect(element.classList.contains("aria-disabled:opacity-50")).toBe(false);
  });

  it("an interactive button keeps the pointer cursor and the aria-disabled dim", () => {
    const element = renderButton({});
    expect(element.getAttribute("aria-disabled")).toBeNull();
    expect(element.classList.contains("cursor-pointer")).toBe(true);
    expect(element.classList.contains("aria-disabled:opacity-50")).toBe(true);
  });

  it("stays a real enabled <button> (not disabled), so hover/focus events still fire", () => {
    const element = renderButton({ readonly: true });
    expect(element.disabled).toBe(false);
  });
});

describe("Button selected option", () => {
  it("swaps the ghost palette for the accent tint, dropping the hover utilities", () => {
    const element = renderButton({ variant: "ghost", selected: true });
    expect(element.classList.contains("bg-accent-light")).toBe(true);
    expect(element.classList.contains("text-accent")).toBe(true);
    const hoverUtilities = Array.from(element.classList).filter((name) => name.includes("hover:"));
    expect(hoverUtilities).toEqual([]);
  });

  it("still passes clicks through", () => {
    const onclick = vi.fn();
    const element = renderButton({ variant: "ghost", selected: true, onclick });
    element.dispatchEvent(new MouseEvent("click"));
    expect(onclick).toHaveBeenCalledTimes(1);
  });
});

// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { NEW_CHAT_COACH_TEXT, NEW_CHAT_PULSE_CLASS, showNewChatCoachMark } from "./newChatCoachMark";

function newChatButton(): HTMLButtonElement {
  const button = document.createElement("button");
  button.className = "chat-rail-new";
  return button;
}

function bubble(): HTMLElement | null {
  return document.querySelector<HTMLElement>(".coach-mark");
}

afterEach(() => {
  document.querySelector<HTMLElement>(".coach-mark-dismiss")?.click();
  document.body.replaceChildren();
});

describe("the coach mark at the New chat button", () => {
  it("points at the rail's button, which pulses until Got it is pressed", async () => {
    const button = newChatButton();
    document.body.appendChild(button);
    const revealList = vi.fn();

    await showNewChatCoachMark(revealList);

    expect(revealList).toHaveBeenCalledTimes(1);
    expect(bubble()?.querySelector(".coach-mark-text")?.textContent).toBe(NEW_CHAT_COACH_TEXT);
    expect(NEW_CHAT_COACH_TEXT).toBe(
      "Start a new chat here any time. Each chat has its own agent, so several can work for you at once.",
    );
    expect(button.classList.contains(NEW_CHAT_PULSE_CLASS)).toBe(true);

    document.querySelector<HTMLElement>(".coach-mark-dismiss")?.click();

    expect(bubble()).toBeNull();
    expect(button.classList.contains(NEW_CHAT_PULSE_CLASS)).toBe(false);
  });

  it("goes when the button itself is clicked", async () => {
    const button = newChatButton();
    document.body.appendChild(button);
    await showNewChatCoachMark(() => undefined);

    button.click();

    expect(bubble()).toBeNull();
    expect(button.classList.contains(NEW_CHAT_PULSE_CLASS)).toBe(false);
  });

  it("opens the phone layout's drawer first, and points at the button the drawer holds", async () => {
    // The phone layout draws no rail: the button exists only once the drawer is open.
    const drawerButton = newChatButton();
    const revealList = vi.fn(() => {
      const drawer = document.createElement("div");
      drawer.className = "chat-drawer-panel";
      drawer.appendChild(drawerButton);
      document.body.appendChild(drawer);
    });

    await showNewChatCoachMark(revealList);

    expect(bubble()).not.toBeNull();
    expect(drawerButton.classList.contains(NEW_CHAT_PULSE_CLASS)).toBe(true);
  });

  it("waits for the drawer's slide to end before it is placed", async () => {
    const drawer = document.createElement("div");
    const button = newChatButton();
    drawer.appendChild(button);
    document.body.appendChild(drawer);
    let endSlide: () => void = () => undefined;
    const slide = { finished: new Promise<void>((resolve) => (endSlide = resolve)) };
    drawer.getAnimations = () => [slide as unknown as Animation];
    button.getAnimations = () => [];

    const shown = showNewChatCoachMark(() => undefined);
    for (let i = 0; i < 10; i++) await Promise.resolve();
    expect(bubble()).toBeNull();

    endSlide();
    await shown;
    expect(bubble()).not.toBeNull();
  });

  it("shows nothing when the list has no New chat button, or the button went while the drawer slid", async () => {
    await showNewChatCoachMark(() => undefined);
    expect(bubble()).toBeNull();

    const button = newChatButton();
    document.body.appendChild(button);
    let endSlide: () => void = () => undefined;
    button.getAnimations = () => [
      { finished: new Promise<void>((resolve) => (endSlide = resolve)) } as unknown as Animation,
    ];
    const shown = showNewChatCoachMark(() => undefined);
    button.remove();
    endSlide();
    await shown;
    expect(bubble()).toBeNull();
  });
});

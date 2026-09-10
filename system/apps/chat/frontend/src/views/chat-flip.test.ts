// @vitest-environment jsdom
/**
 * The two rules that make the flip safe, and one that makes it reachable.
 *
 * Rendered into a real DOM: mithril validates keyed fragments and applies attributes during the
 * DOM diff, so a vnode walk would not see what a browser does.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

import m from "mithril";

import { chatFlipCard } from "./chat-flip";

const ROOT = () => document.getElementById("root") as HTMLElement;

function render(attrs: Parameters<typeof chatFlipCard>[0]): void {
  m.render(ROOT(), chatFlipCard(attrs));
}

const FRONT = m("p", { id: "front" }, "transcript");
const BACK = () => m("iframe", { id: "back", src: "about:blank" });

beforeEach(() => {
  document.body.innerHTML = '<div id="root"></div>';
});

describe("the chat card", () => {
  it("does not build the back face until the first flip", () => {
    // Building it attaches a ttyd client, which sizes the agent's own tmux window. A chat
    // nobody ever turns over should never do that.
    const back = vi.fn(BACK);
    render({ flipped: false, everFlipped: false, front: FRONT, back });
    expect(back).not.toHaveBeenCalled();
    expect(document.querySelector("#back")).toBeNull();
  });

  it("keeps the back face once built, even when the card turns back over", () => {
    // THE bug this guards: mithril destroys a vnode that becomes null, and destroying this one
    // takes the iframe out of the document -- which ends the terminal session rather than
    // hiding it. The sticky flag is why `everFlipped` exists separately from `flipped`.
    render({ flipped: true, everFlipped: true, front: FRONT, back: BACK });
    const iframe = document.querySelector("#back");
    expect(iframe).not.toBeNull();

    render({ flipped: false, everFlipped: true, front: FRONT, back: BACK });
    // Same ELEMENT, not merely another one in the same place: a replaced iframe has reloaded.
    expect(document.querySelector("#back")).toBe(iframe);
  });

  it("hides a face with inert rather than by taking its size away", () => {
    // ttyd sizes the agent's tmux window to its client viewport, so a zero-sized back face
    // hands the agent a zero-column terminal. `display: none` would do exactly that.
    render({ flipped: true, everFlipped: true, front: FRONT, back: BACK });
    const front = document.querySelector(".chat-flip-front") as HTMLElement;
    const back = document.querySelector(".chat-flip-back") as HTMLElement;
    expect(front.hasAttribute("inert")).toBe(true);
    expect(back.hasAttribute("inert")).toBe(false);
    expect(front.style.display).not.toBe("none");
    expect(back.style.display).not.toBe("none");
  });

  it("turns the card by rotating it, and states which way round it is", () => {
    render({ flipped: false, everFlipped: false, front: FRONT, back: BACK });
    const inner = document.querySelector(".chat-flip-inner") as HTMLElement;
    expect(inner.style.transform).toContain("rotateY(0deg)");

    render({ flipped: true, everFlipped: true, front: FRONT, back: BACK });
    expect((document.querySelector(".chat-flip-inner") as HTMLElement).style.transform).toContain("rotateY(180deg)");
  });

  it("puts nothing but the two faces inside the rotating element", () => {
    // The switch that turns the card lives OUTSIDE it. Inside, it would rotate away with the
    // face it turns and the flip would be one-way -- which is why `chatFlipCard` takes no
    // under-bar argument at all, and why this asserts on the shape rather than trusting that.
    render({ flipped: false, everFlipped: true, front: FRONT, back: BACK });
    const inner = document.querySelector(".chat-flip-inner") as HTMLElement;
    const faces = [...inner.children].map((child) => child.className);
    expect(faces).toEqual(["chat-flip-face chat-flip-front", "chat-flip-face chat-flip-back"]);
  });
});

// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import m from "mithril";
import { monogramOf, participantPicture } from "./participant-picture";

describe("monogramOf", () => {
  it("takes the first letter, uppercased, and a question mark for no name", () => {
    expect(monogramOf("bob")).toBe("B");
    expect(monogramOf("  Ana María")).toBe("A");
    expect(monogramOf("   ")).toBe("?");
  });
});

describe("participantPicture", () => {
  function render(node: m.Children): HTMLElement {
    const root = document.createElement("div");
    m.render(root, node);
    return root;
  }

  it("shows the picture when there is one, sized as asked", () => {
    const root = render(participantPicture({ name: "Bob", pictureUrl: "https://p.example/bob.png", size: 24 }));
    const frame = root.querySelector<HTMLElement>("[data-participant-picture]");
    expect(frame?.style.width).toBe("24px");
    expect(frame?.title).toBe("Bob");
    expect(frame?.querySelector("img")?.getAttribute("src")).toBe("https://p.example/bob.png");
  });

  it("falls back to a monogram", () => {
    const root = render(participantPicture({ name: "friend@example.com", pictureUrl: null, size: 32 }));
    expect(root.querySelector("img")).toBeNull();
    expect(root.textContent).toBe("F");
  });

  it("treats an empty picture URL as no picture", () => {
    const root = render(participantPicture({ name: "Bob", pictureUrl: "", size: 24 }));
    expect(root.querySelector("img")).toBeNull();
    expect(root.textContent).toBe("B");
  });

  it("falls back to the monogram when the picture fails to load, and tries a new URL afresh", () => {
    const root = document.createElement("div");
    const draw = (pictureUrl: string): void =>
      m.render(root, participantPicture({ name: "Bob", pictureUrl, size: 24 }));

    draw("https://p.example/gone.png");
    root.querySelector("img")?.dispatchEvent(new Event("error"));
    draw("https://p.example/gone.png");
    expect(root.querySelector("img")).toBeNull();
    expect(root.textContent).toBe("B");

    draw("https://p.example/new.png");
    expect(root.querySelector("img")?.getAttribute("src")).toBe("https://p.example/new.png");
  });
});

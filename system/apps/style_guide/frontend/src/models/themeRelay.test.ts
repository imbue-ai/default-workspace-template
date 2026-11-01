// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";

import { relayTheme, relayToLoadedFrame, THEME_RELAY_ATTRIBUTE } from "./themeRelay";

function frame(isRelaying: boolean): { element: HTMLIFrameElement; post: ReturnType<typeof vi.fn> } {
  const element = document.createElement("iframe");
  if (isRelaying) element.setAttribute(THEME_RELAY_ATTRIBUTE, "");
  document.body.appendChild(element);
  const post = vi.fn();
  Object.defineProperty(element, "contentWindow", { value: { postMessage: post } });
  return { element, post };
}

afterEach(() => {
  for (const element of Array.from(document.body.querySelectorAll("iframe"))) element.remove();
});

describe("the theme relay", () => {
  it("sends the shell's theme on to every relaying frame, as the shell:theme message the page kit follows", () => {
    const relaying = frame(true);
    const other = frame(false);
    relayTheme({ theme: "windows-2000", revision: "r1", isPreview: true });
    expect(relaying.post).toHaveBeenCalledWith(
      { type: "shell:theme", theme: "windows-2000", revision: "r1", isPreview: true },
      window.location.origin,
    );
    expect(other.post).not.toHaveBeenCalled();
  });

  it("sends a frame that loads later the theme the shell last named", () => {
    relayTheme({ theme: "mac-classic", revision: "r2", isPreview: false });
    const late = frame(true);
    relayToLoadedFrame(late.element);
    expect(late.post).toHaveBeenCalledWith(
      { type: "shell:theme", theme: "mac-classic", revision: "r2", isPreview: false },
      window.location.origin,
    );
  });
});

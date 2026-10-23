// @vitest-environment jsdom
/** The page's initial state: read off the script the chat app serves it with, and empty when there is none to read. */
import { afterEach, describe, expect, it, vi } from "vitest";

import { readInitialState } from "./initialState";

function serveWith(text: string): void {
  const script = document.createElement("script");
  script.type = "application/json";
  script.id = "chat-initial-state";
  script.textContent = text;
  document.head.appendChild(script);
}

describe("the page's initial state", () => {
  afterEach(() => {
    document.head.innerHTML = "";
    vi.restoreAllMocks();
  });

  it("is what the page was served with", () => {
    serveWith('{"replay":[{"type":"chats_updated","chats":[],"main_chat_id":null}],"harnesses":{}}');
    expect(readInitialState()).toEqual({
      replay: [{ type: "chats_updated", chats: [], main_chat_id: null }],
      harnesses: {},
    });
  });

  it("is empty on a page served without one", () => {
    expect(readInitialState()).toEqual({});
  });

  it("is empty, with a warning, when what the page carries is not JSON", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    serveWith("{not json");
    expect(readInitialState()).toEqual({});
    expect(warn).toHaveBeenCalledOnce();
  });
});

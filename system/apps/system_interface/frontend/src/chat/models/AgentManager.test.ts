import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { buildAgentTerminalUrl, chatDisplayName } from "./AgentManager";

describe("buildAgentTerminalUrl", () => {
  beforeEach(() => {
    // The terminal app's origin is derived from the page's own location (see src/origin.ts).
    vi.stubGlobal("window", {
      location: { host: "chat-1a2b.host-0af1b2c3d4e5f60718293a4b5c6d7e8f.localhost:8421", protocol: "http:" },
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("attaches to the agent's tmux session on the terminal app's origin, with the args in dispatch order", () => {
    const url = buildAgentTerminalUrl("sunny hollow");
    expect(url.startsWith("http://terminal.host-0af1b2c3d4e5f60718293a4b5c6d7e8f.localhost:8421/?")).toBe(true);
    expect(new URLSearchParams(url.split("?")[1]).getAll("arg")).toEqual(["_", "agent", "sunny hollow"]);
  });
});

describe("chatDisplayName", () => {
  it("prefers the display_name label and falls back to the true name", () => {
    expect(chatDisplayName({ name: "Chat-2", display_name: "Chat 2" })).toBe("Chat 2");
    expect(chatDisplayName({ name: "Chat-2", display_name: "" })).toBe("Chat-2");
    expect(chatDisplayName({ name: "Chat-2" })).toBe("Chat-2");
  });
});

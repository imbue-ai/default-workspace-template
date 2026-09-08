import { afterEach, describe, expect, it, vi } from "vitest";

import { buildAgentTerminalUrl } from "./AgentManager";

describe("buildAgentTerminalUrl", () => {
  function stubPage(terminalLabel: string | null): void {
    // The terminal app's origin is derived from the page's own location (the library's origin.ts) and
    // the label the chat app read out of the registry into the page's meta tag.
    vi.stubGlobal("window", {
      location: { host: "chat-1a2b.host-0af1b2c3d4e5f60718293a4b5c6d7e8f.localhost:8421", protocol: "http:" },
    });
    vi.stubGlobal("document", {
      querySelector: (selector: string) =>
        selector.includes("system-interface-terminal-label") && terminalLabel !== null
          ? { getAttribute: () => terminalLabel }
          : null,
    });
  }

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("attaches to the agent's tmux session on the terminal app's origin, with the args in dispatch order", () => {
    stubPage("terminal");
    const url = buildAgentTerminalUrl("sunny hollow");
    expect(url.startsWith("http://terminal.host-0af1b2c3d4e5f60718293a4b5c6d7e8f.localhost:8421/?")).toBe(true);
    expect(new URLSearchParams(url.split("?")[1]).getAll("arg")).toEqual(["_", "agent", "sunny hollow"]);
  });

  it("uses the label the page carries for the terminal app, and 'terminal' when it carries none", () => {
    stubPage("terminal-2");
    expect(buildAgentTerminalUrl("a").startsWith("http://terminal-2.host-0af1b2c3d4e5f60718293a4b5c6d7e8f.")).toBe(
      true,
    );
    stubPage(null);
    expect(buildAgentTerminalUrl("a").startsWith("http://terminal.host-0af1b2c3d4e5f60718293a4b5c6d7e8f.")).toBe(true);
  });
});

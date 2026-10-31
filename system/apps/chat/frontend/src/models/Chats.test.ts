import { afterEach, describe, expect, it, vi } from "vitest";

import { buildAgentTerminalUrl, isLaunchInFlight, launchChat } from "./Chats";

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

describe("launchChat", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("marks the chat's launch in flight from the post until the backend reports the chat creating, and not after a refusal", async () => {
    // The create route's URL is resolved off the page's base-path meta tag; the page here has none.
    vi.stubGlobal("document", { querySelector: () => null });
    let settle: (response: Response) => void = () => undefined;
    vi.stubGlobal("fetch", () => new Promise<Response>((resolve) => (settle = resolve)));
    const launched = launchChat("agent-7a1b", "acct-1");
    expect(isLaunchInFlight("agent-7a1b")).toBe(true);
    settle(new Response(JSON.stringify({ chat_id: "agent-7a1b", name: "x", display_name: "x" }), { status: 201 }));
    await launched;
    // The backend's own word (its creating push) is what ends the in-flight mark, not the 201.
    expect(isLaunchInFlight("agent-7a1b")).toBe(true);

    vi.stubGlobal(
      "fetch",
      async () => new Response(JSON.stringify({ detail: "not waiting to be launched" }), { status: 400 }),
    );
    await expect(launchChat("agent-9c2d", "acct-1")).rejects.toThrow("not waiting to be launched");
    expect(isLaunchInFlight("agent-9c2d")).toBe(false);
  });
});

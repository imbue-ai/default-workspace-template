// @vitest-environment jsdom
/**
 * The chat page's side of the shell: the chat's own page reports the chat's presence from the
 * shell's messages, a subagent view of the same chat reports nothing (its reports would
 * overwrite the chat page's, keyed on the same chat and client), and the windows the page asks
 * the shell to open beside it are named by path: the chat root's path for a sibling chat, and the
 * three-part subagent key's path for a sub-agent view.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("mithril", () => ({ default: { redraw: vi.fn() } }));
vi.mock("@imbue/workspace-ui/src/base-path", () => ({ apiUrl: (path: string) => path }));
const createChat = vi.fn();
const getChatById = vi.fn();
const addChatsUpdatedListener = vi.fn();
vi.mock("./models/Chats", () => ({ createChat, getChatById, addChatsUpdatedListener }));
vi.mock("./presence", () => ({
  startPresenceReporting: vi.fn(),
  reportPresence: vi.fn(),
  currentPresenceState: vi.fn(() => "hidden"),
}));

import { SHELL_HANDSHAKE, SHELL_HIDDEN, SHELL_LOCATION, SHELL_SHOWN } from "@imbue/workspace-ui/src/app_contract";
import type { ShellConnection } from "@imbue/workspace-ui/src/app_contract";
import { chatSnapshotFixture } from "./models/chatSnapshotFixture";

const HANDSHAKE = {
  type: SHELL_HANDSHAKE,
  clientId: "client-1",
  deviceKind: "desktop",
  viewId: "everything",
  address: "app:chat?instance=agent-1",
  tabId: "chat-agent-1",
};

let connection: ShellConnection | null = null;

/** A fresh copy of the shell module per test: its connection and shown state are module-level.
 *  The presence mock is one object across those copies, so its calls are cleared per test. */
async function loadShell(): Promise<{
  connectChatToShell: typeof import("./shell").connectChatToShell;
  startChatOnAccount: typeof import("./shell").startChatOnAccount;
  openSubagentTab: typeof import("./shell").openSubagentTab;
  presence: { startPresenceReporting: ReturnType<typeof vi.fn>; reportPresence: ReturnType<typeof vi.fn> };
}> {
  vi.resetModules();
  const presence = (await import("./presence")) as unknown as {
    startPresenceReporting: ReturnType<typeof vi.fn>;
    reportPresence: ReturnType<typeof vi.fn>;
  };
  const shell = await import("./shell");
  return {
    connectChatToShell: shell.connectChatToShell,
    startChatOnAccount: shell.startChatOnAccount,
    openSubagentTab: shell.openSubagentTab,
    presence,
  };
}

/** Frame this window under a spy parent for the duration of the test. */
function framed(): { postMessage: ReturnType<typeof vi.fn> } {
  const parent = { postMessage: vi.fn() };
  Object.defineProperty(window, "parent", { value: parent, configurable: true });
  return parent;
}

function deliver(data: unknown, source: unknown): void {
  window.dispatchEvent(new MessageEvent("message", { data, source: source as Window }));
}

beforeEach(() => {
  vi.clearAllMocks();
  getChatById.mockReturnValue(undefined);
});

afterEach(() => {
  vi.unstubAllGlobals();
  connection?.disconnect();
  connection = null;
  delete window.chatPageEmbed;
  Object.defineProperty(window, "parent", { value: window, configurable: true });
});

describe("connectChatToShell", () => {
  it("reports the chat's presence from the shell's messages on the chat's own page", async () => {
    const parent = framed();
    const { connectChatToShell, presence } = await loadShell();
    connection = connectChatToShell("agent-1", { isPresenceReported: true, path: "/agent-1" });

    deliver(HANDSHAKE, parent);
    deliver({ type: SHELL_SHOWN }, parent);
    deliver({ type: SHELL_HIDDEN }, parent);
    window.dispatchEvent(new Event("pagehide"));

    // Hidden until the shell says shown: the page may have loaded into a background tab.
    expect(presence.startPresenceReporting.mock.calls).toEqual([["agent-1", "client-1", "hidden"]]);
    expect(presence.reportPresence.mock.calls).toEqual([["visible"], ["hidden"], ["closed"]]);
  });

  it("reports nothing from a subagent view of the chat", async () => {
    const parent = framed();
    const { connectChatToShell, presence } = await loadShell();
    connection = connectChatToShell("agent-1", { isPresenceReported: false, path: "/agent-1" });

    deliver(HANDSHAKE, parent);
    deliver({ type: SHELL_SHOWN }, parent);
    deliver({ type: SHELL_HIDDEN }, parent);

    expect(presence.startPresenceReporting).not.toHaveBeenCalled();
    expect(presence.reportPresence).not.toHaveBeenCalled();
  });

  it("still tells the shell where its tab is from a subagent view", async () => {
    const parent = framed();
    const { connectChatToShell } = await loadShell();
    connection = connectChatToShell("agent-1", { isPresenceReported: false, path: "/agent-1" });

    window.dispatchEvent(new Event("focus"));

    expect(parent.postMessage).toHaveBeenCalledWith({ type: "shell:focused" }, "*");
  });
});

/** The location reports the page posted, as ``[path, title]`` pairs. */
function locationReports(parent: { postMessage: ReturnType<typeof vi.fn> }): [string, string][] {
  return parent.postMessage.mock.calls
    .filter(([message]) => (message as { type: string }).type === SHELL_LOCATION)
    .map(([message]) => {
      const { path, title } = message as { path: string; title: string };
      return [path, title];
    });
}

describe("the chat page's location report", () => {
  it("reports its path on connect, and the chat's title once the list names it, once per change", async () => {
    const parent = framed();
    const { connectChatToShell } = await loadShell();
    connection = connectChatToShell("agent-1", { isPresenceReported: true, path: "/agent-1" });
    expect(locationReports(parent)).toEqual([["/agent-1", ""]]);
    const onChatsUpdated = addChatsUpdatedListener.mock.calls[0][0] as () => void;

    getChatById.mockReturnValue(chatSnapshotFixture("agent-1", { title: "Plan" }));
    onChatsUpdated();
    onChatsUpdated();

    expect(locationReports(parent)).toEqual([
      ["/agent-1", ""],
      ["/agent-1", "Plan"],
    ]);
    expect(document.title).toBe("Plan");
  });
});

describe("the embed API", () => {
  it("drives a framed page's presence the way the shell's messages do", async () => {
    framed();
    const { connectChatToShell, presence } = await loadShell();
    connection = connectChatToShell("agent-1", { isPresenceReported: true, path: "/agent-1" });
    const embed = window.chatPageEmbed;
    expect(embed).toBeDefined();
    if (embed === undefined) throw new Error("no embed API on a framed page");

    embed.handshake({
      clientId: "client-2",
      windowId: "",
      desktopId: "",
      path: "",
      deviceKind: "",
      viewId: "",
      address: "",
      tabId: "",
    });
    embed.shown();
    embed.hidden();

    expect(presence.startPresenceReporting.mock.calls).toEqual([["agent-1", "client-2", "hidden"]]);
    expect(presence.reportPresence.mock.calls).toEqual([["visible"], ["hidden"]]);
  });

  it("is absent on a top-level visit, which no root drives", async () => {
    const { connectChatToShell } = await loadShell();
    connection = connectChatToShell("agent-1", { isPresenceReported: true, path: "/agent-1" });

    expect(window.chatPageEmbed).toBeUndefined();
  });
});

describe("startChatOnAccount", () => {
  it("asks the shell to open the new chat at the root's path for it", async () => {
    const parent = framed();
    const { connectChatToShell, startChatOnAccount } = await loadShell();
    connection = connectChatToShell("agent-1", { isPresenceReported: false, path: "/agent-1" });
    deliver(HANDSHAKE, parent);
    createChat.mockResolvedValueOnce({ chatId: "agent-2", name: "Chat-2", displayName: "Chat 2" });

    await startChatOnAccount("account-1");

    expect(createChat).toHaveBeenCalledWith("", "account-1", "", null);
    expect(parent.postMessage).toHaveBeenCalledWith(
      { type: "shell:open", path: "/?chat=agent-2", ifPresent: "focus" },
      "*",
    );
  });

  it("files the new chat in no project: the desktop shell's view id is a desktop, not a project", async () => {
    const parent = framed();
    const { connectChatToShell, startChatOnAccount } = await loadShell();
    connection = connectChatToShell("agent-1", { isPresenceReported: false, path: "/agent-1" });
    deliver({ ...HANDSHAKE, viewId: "home", desktopId: "home", windowId: "win-1", path: "/agent-1" }, parent);
    createChat.mockResolvedValueOnce({ chatId: "agent-2", name: "Chat-2", displayName: "Chat 2" });

    await startChatOnAccount("account-1");

    expect(createChat).toHaveBeenCalledWith("", "account-1", "", null);
  });

  it("passes a first message through and reports whether the chat opened", async () => {
    const parent = framed();
    const { connectChatToShell, startChatOnAccount } = await loadShell();
    connection = connectChatToShell("agent-1", { isPresenceReported: false, path: "/agent-1" });
    deliver(HANDSHAKE, parent);
    createChat.mockResolvedValueOnce({ chatId: "agent-2", name: "Chat-2", displayName: "Chat 2" });
    expect(await startChatOnAccount("account-1", "Carry on here")).toBe(true);
    expect(createChat).toHaveBeenCalledWith("", "account-1", "Carry on here", null);

    const alertSpy = vi.spyOn(window, "alert").mockImplementation(() => undefined);
    createChat.mockRejectedValueOnce(new Error("no usable account"));
    expect(await startChatOnAccount("account-1", "Carry on here")).toBe(false);
    alertSpy.mockRestore();
  });

  it("tells the user when the create fails rather than opening nothing silently", async () => {
    const parent = framed();
    const { connectChatToShell, startChatOnAccount } = await loadShell();
    connection = connectChatToShell("agent-1", { isPresenceReported: false, path: "/agent-1" });
    const alertSpy = vi.spyOn(window, "alert").mockImplementation(() => undefined);
    createChat.mockRejectedValueOnce(new Error("no usable account"));

    await startChatOnAccount("account-1");

    expect(alertSpy).toHaveBeenCalledWith("Failed to create chat: no usable account");
    expect(parent.postMessage).not.toHaveBeenCalledWith(expect.objectContaining({ type: "shell:open" }), "*");
    alertSpy.mockRestore();
  });
});

describe("openSubagentTab", () => {
  /** A chat app whose instances route accepts every subagent create, answering the record it made. */
  function acceptingInstancesRoute(key: string): ReturnType<typeof vi.fn> {
    const fetchSpy = vi.fn(async () => ({ ok: true, status: 200, json: async () => ({ key }) }));
    vi.stubGlobal("fetch", fetchSpy);
    return fetchSpy;
  }

  /** A chat app whose instances route refuses every subagent create. */
  function refusingInstancesRoute(): void {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: false, status: 503, json: async () => ({ detail: "not yet" }) })),
    );
  }

  it("creates the view under the chat, then asks the shell to open the key the app answered", async () => {
    const parent = framed();
    // The app keys the view on the agent it holds as active, which may not be the one the
    // page's last snapshot showed.
    const fetchSpy = acceptingInstancesRoute("agent-1.agent-7.sess-3");
    const { connectChatToShell, openSubagentTab } = await loadShell();
    connection = connectChatToShell("agent-1", { isPresenceReported: false, path: "/agent-1" });
    deliver(HANDSHAKE, parent);
    getChatById.mockReturnValue(chatSnapshotFixture("agent-1", { active_agent: { agent_id: "agent-9" } }));

    await openSubagentTab("agent-1", "sess-3", "Explore the repo");

    expect(fetchSpy).toHaveBeenCalledWith(
      "/_instances",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          action: "subagent",
          params: { parent: "agent-1", session: "sess-3", description: "Explore the repo" },
        }),
      }),
    );
    expect(parent.postMessage).toHaveBeenCalledWith(
      { type: "shell:open", path: "/agent-1.agent-7.sess-3", ifPresent: "focus" },
      "*",
    );
  });

  it("falls back to the listed active agent's key when the create is refused", async () => {
    const parent = framed();
    refusingInstancesRoute();
    const { connectChatToShell, openSubagentTab } = await loadShell();
    connection = connectChatToShell("agent-1", { isPresenceReported: false, path: "/agent-1" });
    deliver(HANDSHAKE, parent);
    getChatById.mockReturnValue(chatSnapshotFixture("agent-1", { active_agent: { agent_id: "agent-9" } }));

    await openSubagentTab("agent-1", "sess-3", "Explore the repo");

    expect(parent.postMessage).toHaveBeenCalledWith(
      { type: "shell:open", path: "/agent-1.agent-9.sess-3", ifPresent: "focus" },
      "*",
    );
  });

  it("keys the view on the chat's own id while the page does not list the chat yet and the create is refused", async () => {
    const parent = framed();
    refusingInstancesRoute();
    const { connectChatToShell, openSubagentTab } = await loadShell();
    connection = connectChatToShell("agent-1", { isPresenceReported: false, path: "/agent-1" });
    deliver(HANDSHAKE, parent);

    await openSubagentTab("agent-1", "sess-3", "Explore the repo");

    expect(parent.postMessage).toHaveBeenCalledWith(
      { type: "shell:open", path: "/agent-1.agent-1.sess-3", ifPresent: "focus" },
      "*",
    );
  });
});

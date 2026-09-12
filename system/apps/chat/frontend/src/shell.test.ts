// @vitest-environment jsdom
/**
 * The chat page's side of the shell: the chat's own page reports the chat's presence from the
 * shell's messages, a subagent view of the same chat reports nothing (its reports would
 * overwrite the chat page's, keyed on the same chat and client), and the tabs the page asks
 * the shell to open beside it are addressed by chat id and by the three-part subagent key.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("mithril", () => ({ default: { redraw: vi.fn() } }));
vi.mock("@imbue/workspace-ui/src/base-path", () => ({ apiUrl: (path: string) => path }));
const createChat = vi.fn();
const getChatById = vi.fn();
vi.mock("./models/Chats", () => ({ createChat, getChatById }));
vi.mock("./presence", () => ({
  startPresenceReporting: vi.fn(),
  reportPresence: vi.fn(),
  currentPresenceState: vi.fn(() => "hidden"),
}));

import { SHELL_HANDSHAKE, SHELL_HIDDEN, SHELL_SHOWN } from "@imbue/workspace-ui/src/app_contract";
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
  Object.defineProperty(window, "parent", { value: window, configurable: true });
});

describe("connectChatToShell", () => {
  it("reports the chat's presence from the shell's messages on the chat's own page", async () => {
    const parent = framed();
    const { connectChatToShell, presence } = await loadShell();
    connection = connectChatToShell("agent-1", { isPresenceReported: true });

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
    connection = connectChatToShell("agent-1", { isPresenceReported: false });

    deliver(HANDSHAKE, parent);
    deliver({ type: SHELL_SHOWN }, parent);
    deliver({ type: SHELL_HIDDEN }, parent);

    expect(presence.startPresenceReporting).not.toHaveBeenCalled();
    expect(presence.reportPresence).not.toHaveBeenCalled();
  });

  it("still tells the shell where its tab is from a subagent view", async () => {
    const parent = framed();
    const { connectChatToShell } = await loadShell();
    connection = connectChatToShell("agent-1", { isPresenceReported: false });

    window.dispatchEvent(new Event("focus"));

    expect(parent.postMessage).toHaveBeenCalledWith({ type: "shell:focused" }, "*");
  });
});

describe("startChatOnAccount", () => {
  it("asks the shell to open the new chat beside this one", async () => {
    const parent = framed();
    const { connectChatToShell, startChatOnAccount } = await loadShell();
    connection = connectChatToShell("agent-1", { isPresenceReported: false });
    deliver(HANDSHAKE, parent);
    createChat.mockResolvedValueOnce({ chatId: "agent-2", name: "Chat-2", displayName: "Chat 2" });

    await startChatOnAccount("account-1");

    // Started from Everything, the chat is filed in no project.
    expect(createChat).toHaveBeenCalledWith("", "account-1");
    expect(parent.postMessage).toHaveBeenCalledWith({ type: "shell:open", address: "app:chat?instance=agent-2" }, "*");
  });

  it("files the new chat in the project this one is shown in", async () => {
    const parent = framed();
    const { connectChatToShell, startChatOnAccount } = await loadShell();
    connection = connectChatToShell("agent-1", { isPresenceReported: false });
    deliver({ ...HANDSHAKE, viewId: "project-7" }, parent);
    createChat.mockResolvedValueOnce({ chatId: "agent-2", name: "Chat-2", displayName: "Chat 2" });

    await startChatOnAccount("account-1");

    expect(createChat).toHaveBeenCalledWith("project-7", "account-1");
  });

  it("tells the user when the create fails rather than opening nothing silently", async () => {
    const parent = framed();
    const { connectChatToShell, startChatOnAccount } = await loadShell();
    connection = connectChatToShell("agent-1", { isPresenceReported: false });
    const alertSpy = vi.spyOn(window, "alert").mockImplementation(() => undefined);
    createChat.mockRejectedValueOnce(new Error("no usable account"));

    await startChatOnAccount("account-1");

    expect(alertSpy).toHaveBeenCalledWith("Failed to create chat: no usable account");
    expect(parent.postMessage).not.toHaveBeenCalledWith(expect.objectContaining({ type: "shell:open" }), "*");
    alertSpy.mockRestore();
  });
});

describe("openSubagentTab", () => {
  /** A chat app whose instances route accepts every subagent create. */
  function acceptingInstancesRoute(): ReturnType<typeof vi.fn> {
    const fetchSpy = vi.fn(async () => ({ ok: true, status: 200, json: async () => ({}) }));
    vi.stubGlobal("fetch", fetchSpy);
    return fetchSpy;
  }

  it("creates the view under the chat and its active agent, then asks the shell to open it", async () => {
    const parent = framed();
    const fetchSpy = acceptingInstancesRoute();
    const { connectChatToShell, openSubagentTab } = await loadShell();
    connection = connectChatToShell("agent-1", { isPresenceReported: false });
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
      { type: "shell:open", address: "app:chat?instance=agent-1.agent-9.sess-3" },
      "*",
    );
  });

  it("keys the view on the chat's own id while the page does not list the chat yet", async () => {
    const parent = framed();
    acceptingInstancesRoute();
    const { connectChatToShell, openSubagentTab } = await loadShell();
    connection = connectChatToShell("agent-1", { isPresenceReported: false });
    deliver(HANDSHAKE, parent);

    await openSubagentTab("agent-1", "sess-3", "Explore the repo");

    expect(parent.postMessage).toHaveBeenCalledWith(
      { type: "shell:open", address: "app:chat?instance=agent-1.agent-1.sess-3" },
      "*",
    );
  });
});

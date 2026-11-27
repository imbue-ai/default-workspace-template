// @vitest-environment jsdom
/**
 * The chat page's side of the shell: the chat's own page reports the chat's presence from the
 * shell's messages, a subagent view of the same chat reports nothing (its reports would
 * overwrite the chat page's, keyed on the same chat and client), and the windows the page asks
 * the shell to open beside it are named by path: the chat root's path for a sibling chat, and the
 * three-part subagent key's path for a sub-agent view.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MockInstance } from "vitest";

vi.mock("mithril", () => ({ default: { redraw: vi.fn() } }));
vi.mock("@imbue/workspace-ui/src/base-path", () => ({ apiUrl: (path: string) => path }));
const createChat = vi.fn();
const getChatById = vi.fn();
const addChatsUpdatedListener = vi.fn();
vi.mock("./models/Chats", () => ({
  createChat,
  getChatById,
  addChatsUpdatedListener,
  addHeldSendFailedListener: () => undefined,
}));
vi.mock("./presence", () => ({
  startPresenceReporting: vi.fn(),
  reportPresence: vi.fn(),
  reportFocusChange: vi.fn(),
  currentPresenceState: vi.fn(() => "hidden"),
}));
const ensureParticipants = vi.fn(() => Promise.resolve());
vi.mock("./models/Participants", () => ({ ensureParticipants }));
const rereadNextStepSuggestions = vi.fn();
vi.mock("./models/Suggestions", () => ({ rereadNextStepSuggestions }));

import { CHAT_ON_SCREEN, PROVIDER_CHOOSER_ON_SCREEN } from "@imbue/workspace-ui/src/embed";
import { SHELL_HANDSHAKE, SHELL_HIDDEN, SHELL_LOCATION, SHELL_SHOWN } from "@imbue/workspace-ui/src/app_contract";
import type { ShellConnection } from "@imbue/workspace-ui/src/app_contract";
import { chatSnapshotFixture } from "./models/chatSnapshotFixture";

const HANDSHAKE = {
  type: SHELL_HANDSHAKE,
  clientId: "client-1",
  windowId: "win-1",
  desktopId: "home",
  app: "chat",
  path: "/agent-1",
};

let connection: ShellConnection | null = null;

/** A fresh copy of the shell module per test: its connection and shown state are module-level.
 *  The presence mock is one object across those copies, so its calls are cleared per test. */
async function loadShell(): Promise<{
  connectChatToShell: typeof import("./shell").connectChatToShell;
  reportChatOnScreenOnce: typeof import("./shell").reportChatOnScreenOnce;
  reportProviderChooserOnScreenOnce: typeof import("./shell").reportProviderChooserOnScreenOnce;
  startChatOnAccount: typeof import("./shell").startChatOnAccount;
  openSubagentView: typeof import("./shell").openSubagentView;
  presence: {
    startPresenceReporting: ReturnType<typeof vi.fn>;
    reportPresence: ReturnType<typeof vi.fn>;
    reportFocusChange: ReturnType<typeof vi.fn>;
  };
}> {
  vi.resetModules();
  const presence = (await import("./presence")) as unknown as {
    startPresenceReporting: ReturnType<typeof vi.fn>;
    reportPresence: ReturnType<typeof vi.fn>;
    reportFocusChange: ReturnType<typeof vi.fn>;
  };
  const shell = await import("./shell");
  return {
    connectChatToShell: shell.connectChatToShell,
    reportChatOnScreenOnce: shell.reportChatOnScreenOnce,
    reportProviderChooserOnScreenOnce: shell.reportProviderChooserOnScreenOnce,
    startChatOnAccount: shell.startChatOnAccount,
    openSubagentView: shell.openSubagentView,
    presence,
  };
}

/** Frame this window under a spy parent for the duration of the test. */
function framed(): { postMessage: ReturnType<typeof vi.fn> } {
  const parent = { postMessage: vi.fn() };
  Object.defineProperty(window, "parent", { value: parent, configurable: true });
  return parent;
}

/** The window listeners `connect` adds, by event type: called directly, so listeners an earlier test left on the
 *  shared window do not answer too. */
function windowListenersAddedBy(connect: () => void): (type: string) => Array<(event: Event) => void> {
  const addSpy = vi.spyOn(window, "addEventListener");
  connect();
  const added = addSpy.mock.calls.map(([type, listener]) => [type, listener] as const);
  addSpy.mockRestore();
  return (type) =>
    added.filter(([addedType]) => addedType === type).map(([, listener]) => listener as (event: Event) => void);
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

  it.each([
    ["framed", true],
    ["visited directly", false],
  ])("reports the document's focus as it comes and goes on the chat's own page, %s", async (_how, isFramed) => {
    if (isFramed) framed();
    const { connectChatToShell, presence } = await loadShell();
    const windowListeners = windowListenersAddedBy(() => {
      connection = connectChatToShell("agent-1", { isPresenceReported: true, path: "/agent-1" });
    });

    windowListeners("focus").forEach((listener) => listener(new Event("focus")));
    windowListeners("blur").forEach((listener) => listener(new Event("blur")));

    expect(presence.reportFocusChange).toHaveBeenCalledTimes(2);
  });

  it("reports no focus from a subagent view of the chat", async () => {
    framed();
    const { connectChatToShell, presence } = await loadShell();
    const windowListeners = windowListenersAddedBy(() => {
      connection = connectChatToShell("agent-1", { isPresenceReported: false, path: "/agent-1" });
    });

    windowListeners("focus").forEach((listener) => listener(new Event("focus")));
    windowListeners("blur").forEach((listener) => listener(new Event("blur")));

    expect(presence.reportFocusChange).not.toHaveBeenCalled();
  });

  it("still tells the shell where its window is from a subagent view", async () => {
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

describe("reportChatOnScreenOnce", () => {
  function chatOnScreenReports(parent: { postMessage: ReturnType<typeof vi.fn> }): unknown[] {
    return parent.postMessage.mock.calls.map(([data]) => data).filter((data) => data.type === CHAT_ON_SCREEN);
  }

  let layoutSpy: MockInstance | null = null;

  /** Give the document a height (jsdom lays nothing out). */
  function layOutDocument(height: number): void {
    layoutSpy = vi.spyOn(document.documentElement, "getBoundingClientRect").mockReturnValue({ height } as DOMRect);
  }

  afterEach(() => {
    layoutSpy?.mockRestore();
    layoutSpy = null;
  });

  it("reports a framed chat once it is both shown and loaded, and only once", async () => {
    const parent = framed();
    layOutDocument(900);
    const { connectChatToShell, reportChatOnScreenOnce } = await loadShell();
    connection = connectChatToShell("agent-1", { isPresenceReported: true, path: "/agent-1" });

    reportChatOnScreenOnce("agent-1", true);
    expect(chatOnScreenReports(parent)).toEqual([]);

    deliver(HANDSHAKE, parent);
    deliver({ type: SHELL_SHOWN }, parent);
    reportChatOnScreenOnce("agent-1", false);
    expect(chatOnScreenReports(parent)).toEqual([]);

    reportChatOnScreenOnce("agent-1", true);
    reportChatOnScreenOnce("agent-1", true);
    expect(chatOnScreenReports(parent)).toEqual([{ type: CHAT_ON_SCREEN, chatId: "agent-1" }]);
  });

  it("does not count a page whose frame is not laid out", async () => {
    const parent = framed();
    layOutDocument(0);
    const { connectChatToShell, reportChatOnScreenOnce } = await loadShell();
    connection = connectChatToShell("agent-1", { isPresenceReported: true, path: "/agent-1" });
    deliver(HANDSHAKE, parent);
    deliver({ type: SHELL_SHOWN }, parent);

    reportChatOnScreenOnce("agent-1", true);
    expect(chatOnScreenReports(parent)).toEqual([]);
  });
});

describe("reportProviderChooserOnScreenOnce", () => {
  function chooserReports(parent: { postMessage: ReturnType<typeof vi.fn> }): unknown[] {
    return parent.postMessage.mock.calls
      .map(([data]) => data)
      .filter((data) => data.type === PROVIDER_CHOOSER_ON_SCREEN);
  }

  let layoutSpy: MockInstance | null = null;

  afterEach(() => {
    layoutSpy?.mockRestore();
    layoutSpy = null;
  });

  it("reports once the root is shown and laid out, with no chat named, and only once", async () => {
    const parent = framed();
    layoutSpy = vi.spyOn(document.documentElement, "getBoundingClientRect").mockReturnValue({ height: 0 } as DOMRect);
    const { reportProviderChooserOnScreenOnce } = await loadShell();

    reportProviderChooserOnScreenOnce(false);
    reportProviderChooserOnScreenOnce(true);
    expect(chooserReports(parent)).toEqual([]);

    layoutSpy.mockReturnValue({ height: 900 } as DOMRect);
    reportProviderChooserOnScreenOnce(false);
    expect(chooserReports(parent)).toEqual([]);

    reportProviderChooserOnScreenOnce(true);
    reportProviderChooserOnScreenOnce(true);
    expect(chooserReports(parent)).toEqual([{ type: PROVIDER_CHOOSER_ON_SCREEN }]);
  });
});

describe("whether the chrome opens its workspace settings", () => {
  const CAPABILITIES = { type: "minds:embedder-capabilities", canPopOut: false };

  async function loadConnectedShell(): Promise<typeof import("./shell")> {
    vi.resetModules();
    const shell = await import("./shell");
    connection = shell.connectChatToShell("agent-1", { isPresenceReported: true, path: "/agent-1" });
    return shell;
  }

  it("is no until a handshake says so, for a page greeted after the chrome spoke or framed by the chat root", async () => {
    const parent = framed();
    const shell = await loadConnectedShell();
    expect(shell.opensWorkspaceSettings()).toBe(false);

    deliver(HANDSHAKE, parent);
    expect(shell.opensWorkspaceSettings()).toBe(false);

    // The root hands its pages the shell's handshake again when the chrome speaks after it.
    deliver({ ...HANDSHAKE, opensWorkspaceSettings: true }, parent);
    expect(shell.opensWorkspaceSettings()).toBe(true);
  });

  it("follows the chrome's own word, which the shell passes on to a page it already greeted", async () => {
    const parent = framed();
    const shell = await loadConnectedShell();
    deliver(HANDSHAKE, parent);

    deliver({ ...CAPABILITIES, opensWorkspaceSettings: true }, parent);
    expect(shell.opensWorkspaceSettings()).toBe(true);

    deliver(CAPABILITIES, parent);
    expect(shell.opensWorkspaceSettings()).toBe(false);
  });

  it("asks the chrome for its workspace settings at a group with the contract's message", async () => {
    const parent = framed();
    const shell = await loadConnectedShell();

    shell.openWorkspaceSettings("suggestions");

    expect(parent.postMessage.mock.calls.map(([data]) => data)).toContainEqual({
      type: "minds:open-workspace-settings",
      group: "suggestions",
    });
  });
});

describe("whether the chrome draws coach marks on its own controls", () => {
  const CAPABILITIES = { type: "minds:embedder-capabilities", canPopOut: false };

  async function loadConnectedShell(): Promise<typeof import("./shell")> {
    vi.resetModules();
    const shell = await import("./shell");
    connection = shell.connectChatToShell("agent-1", { isPresenceReported: true, path: "/agent-1" });
    return shell;
  }

  it("is no until a handshake says so, and follows the chrome's own word after it", async () => {
    const parent = framed();
    const shell = await loadConnectedShell();
    expect(shell.showsCoachMarks()).toBe(false);

    deliver(HANDSHAKE, parent);
    expect(shell.showsCoachMarks()).toBe(false);

    deliver({ ...HANDSHAKE, showsCoachMarks: true }, parent);
    expect(shell.showsCoachMarks()).toBe(true);

    deliver(CAPABILITIES, parent);
    expect(shell.showsCoachMarks()).toBe(false);

    deliver({ ...CAPABILITIES, showsCoachMarks: true }, parent);
    expect(shell.showsCoachMarks()).toBe(true);
  });

  it("asks the chrome for its share settings on the whole workspace, with a coach mark at one control", async () => {
    const parent = framed();
    const shell = await loadConnectedShell();

    shell.openShareSettingsWithCoachMark("mobile-access");

    // No serviceName: the tab opens on the workspace, not on one app.
    expect(parent.postMessage.mock.calls.map(([data]) => data)).toContainEqual({
      type: "minds:open-share-settings",
      coachMark: "mobile-access",
    });
  });

  it("asks the shell for a coach card under a control of the chrome", async () => {
    const parent = framed();
    const shell = await loadConnectedShell();

    shell.showCoachCard("Your connections live here.", "permissions");

    expect(parent.postMessage.mock.calls.map(([data]) => data)).toContainEqual({
      type: "shell:show-coach-card",
      text: "Your connections live here.",
      chromeAnchor: "permissions",
    });
  });
});

describe("what the page tells the shell of its chat's agent", () => {
  async function loadConnectedShell(): Promise<typeof import("./shell")> {
    vi.resetModules();
    const shell = await import("./shell");
    connection = shell.connectChatToShell("agent-1", { isPresenceReported: true, path: "/agent-1" });
    return shell;
  }

  function reports(parent: { postMessage: ReturnType<typeof vi.fn> }): unknown[] {
    return parent.postMessage.mock.calls
      .map(([data]) => data as { type?: string })
      .filter((data) => data.type === "shell:agent-working");
  }

  it("says an agent is working only when that has changed, and nothing while none has worked yet", async () => {
    const parent = framed();
    const shell = await loadConnectedShell();

    shell.reportAgentWorking(false);
    expect(reports(parent)).toEqual([]);

    shell.reportAgentWorking(true);
    shell.reportAgentWorking(true);
    shell.reportAgentWorking(false);
    shell.reportAgentWorking(false);

    expect(reports(parent)).toEqual([
      { type: "shell:agent-working", isWorking: true },
      { type: "shell:agent-working", isWorking: false },
    ]);
  });

  it("says so again to a shell that greets it while an agent works, and not once the agent has stopped", async () => {
    const parent = framed();
    const shell = await loadConnectedShell();
    deliver(HANDSHAKE, parent);
    expect(reports(parent)).toEqual([]);

    shell.reportAgentWorking(true);
    deliver(HANDSHAKE, parent);
    expect(reports(parent)).toEqual([
      { type: "shell:agent-working", isWorking: true },
      { type: "shell:agent-working", isWorking: true },
    ]);

    shell.reportAgentWorking(false);
    deliver(HANDSHAKE, parent);
    expect(reports(parent)).toHaveLength(3);
  });
});

describe("whether the chrome said it relays no provider sign-in", () => {
  const CAPABILITIES = { type: "minds:embedder-capabilities", canPopOut: false };

  it("is no until a handshake says so, and follows the chrome's own word after it", async () => {
    const parent = framed();
    vi.resetModules();
    const shell = await import("./shell");
    const relay = await import("./models/providerRelay");
    connection = shell.connectChatToShell("agent-1", { isPresenceReported: true, path: "/agent-1" });
    expect(relay.isProviderRelayDeclined()).toBe(false);

    deliver(HANDSHAKE, parent);
    expect(relay.isProviderRelayDeclined()).toBe(false);

    deliver({ ...HANDSHAKE, declinesProviderRelay: true }, parent);
    expect(relay.isProviderRelayDeclined()).toBe(true);

    deliver(CAPABILITIES, parent);
    expect(relay.isProviderRelayDeclined()).toBe(false);

    deliver({ ...CAPABILITIES, declinesProviderRelay: true }, parent);
    expect(relay.isProviderRelayDeclined()).toBe(true);
  });
});

describe("the chat's suggestions when its page is shown again", () => {
  it("are read again when the shell shows the page", async () => {
    const parent = framed();
    const { connectChatToShell } = await loadShell();
    connection = connectChatToShell("agent-1", { isPresenceReported: true, path: "/agent-1" });
    deliver(HANDSHAKE, parent);
    expect(rereadNextStepSuggestions).not.toHaveBeenCalled();

    deliver({ type: SHELL_SHOWN }, parent);

    expect(rereadNextStepSuggestions.mock.calls).toEqual([["agent-1"]]);
  });

  it("are read again when a page visited directly becomes visible", async () => {
    const { connectChatToShell } = await loadShell();
    const addSpy = vi.spyOn(document, "addEventListener");
    connection = connectChatToShell("agent-1", { isPresenceReported: true, path: "/agent-1" });
    const onVisibilityChange = addSpy.mock.calls.find(([type]) => type === "visibilitychange")?.[1] as () => void;
    addSpy.mockRestore();
    const visibility = vi.spyOn(document, "visibilityState", "get");

    visibility.mockReturnValue("hidden");
    onVisibilityChange();
    expect(rereadNextStepSuggestions).not.toHaveBeenCalled();

    visibility.mockReturnValue("visible");
    onVisibilityChange();
    expect(rereadNextStepSuggestions.mock.calls).toEqual([["agent-1"]]);
    visibility.mockRestore();
  });
});

describe("the embed API", () => {
  it("drives a framed page's presence the way the shell's messages do", async () => {
    const parent = framed();
    const { connectChatToShell, presence } = await loadShell();
    connection = connectChatToShell("agent-1", { isPresenceReported: true, path: "/agent-1" });
    const embed = window.chatPageEmbed;
    expect(embed).toBeDefined();
    if (embed === undefined) throw new Error("no embed API on a framed page");

    deliver({ type: SHELL_HANDSHAKE, clientId: "client-2" }, parent);
    ensureParticipants.mockClear();
    embed.shown();
    embed.hidden();

    expect(presence.startPresenceReporting.mock.calls).toEqual([["agent-1", "client-2", "hidden"]]);
    expect(presence.reportPresence.mock.calls).toEqual([["visible"], ["hidden"]]);
    // Coming back into view reads the roster again, so whoever joined meanwhile is drawn right; so does coming
    // into focus (the window keeps every earlier test's focus listener too, so only the growth is counted).
    expect(ensureParticipants).toHaveBeenCalledTimes(1);
    const readsBeforeFocus = ensureParticipants.mock.calls.length;
    window.dispatchEvent(new Event("focus"));
    expect(ensureParticipants.mock.calls.length).toBeGreaterThan(readsBeforeFocus);
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

    // The create names the client the shell's handshake gave this page: a person asked for the chat.
    expect(createChat).toHaveBeenCalledWith("", "account-1", "", null, "client-1");
    expect(parent.postMessage).toHaveBeenCalledWith(
      { type: "shell:open", path: "/?chat=agent-2", ifPresent: "focus" },
      "*",
    );
  });

  it("files the new chat in no project: the desktop has none", async () => {
    const parent = framed();
    const { connectChatToShell, startChatOnAccount } = await loadShell();
    connection = connectChatToShell("agent-1", { isPresenceReported: false, path: "/agent-1" });
    deliver(HANDSHAKE, parent);
    createChat.mockResolvedValueOnce({ chatId: "agent-2", name: "Chat-2", displayName: "Chat 2" });

    await startChatOnAccount("account-1");

    expect(createChat).toHaveBeenCalledWith("", "account-1", "", null, "client-1");
  });

  it("passes a first message through and reports whether the chat opened", async () => {
    const parent = framed();
    const { connectChatToShell, startChatOnAccount } = await loadShell();
    connection = connectChatToShell("agent-1", { isPresenceReported: false, path: "/agent-1" });
    deliver(HANDSHAKE, parent);
    createChat.mockResolvedValueOnce({ chatId: "agent-2", name: "Chat-2", displayName: "Chat 2" });
    expect(await startChatOnAccount("account-1", "Carry on here")).toBe(true);
    expect(createChat).toHaveBeenCalledWith("", "account-1", "Carry on here", null, "client-1");

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

describe("openSubagentView", () => {
  it("asks the shell to open the view keyed on the chat's active agent", async () => {
    const parent = framed();
    const { connectChatToShell, openSubagentView } = await loadShell();
    connection = connectChatToShell("agent-1", { isPresenceReported: false, path: "/agent-1" });
    deliver(HANDSHAKE, parent);
    getChatById.mockReturnValue(chatSnapshotFixture("agent-1", { active_agent: { agent_id: "agent-9" } }));

    openSubagentView("agent-1", "sess-3");

    expect(parent.postMessage).toHaveBeenCalledWith(
      { type: "shell:open", path: "/agent-1.agent-9.sess-3", ifPresent: "focus" },
      "*",
    );
  });

  it("keys the view on the chat's own id while the page does not list the chat yet", async () => {
    const parent = framed();
    const { connectChatToShell, openSubagentView } = await loadShell();
    connection = connectChatToShell("agent-1", { isPresenceReported: false, path: "/agent-1" });
    deliver(HANDSHAKE, parent);

    openSubagentView("agent-1", "sess-3");

    expect(parent.postMessage).toHaveBeenCalledWith(
      { type: "shell:open", path: "/agent-1.agent-1.sess-3", ifPresent: "focus" },
      "*",
    );
  });
});

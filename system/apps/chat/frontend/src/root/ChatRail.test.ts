// @vitest-environment jsdom
/**
 * The rail's status marks: a chat waiting on a background task wears the dashed ring, a working one the breathing
 * dot (pending tasks or not, since the backend reports it working), and an idle one the hollow ring. And the
 * main chat: it leads in its own group, set off from the rest, and its row is marked.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

const verbs = vi.hoisted(() => ({
  stopChat: vi.fn(async (_chatId: string) => undefined),
  startChat: vi.fn(async (_chatId: string) => undefined),
  renameChat: vi.fn(async (_chatId: string, _title: string) => undefined),
  makeMainChat: vi.fn(async (_chatId: string) => undefined),
  destroyChat: vi.fn(async (_chatId: string) => undefined),
}));
vi.mock("./verbs", () => verbs);

import m from "mithril";
import {
  HOST_MENU_GLOBAL,
  installElementContextMenu,
  type HostMenuBridge,
} from "@imbue/workspace-ui/src/context_menu";
import { setParticipantsForTesting } from "../models/Participants";
import { ChatRail, railContextMenuRoutes, rowMenuRows } from "./ChatRail";
import { chatRailAttrsFixture } from "./chatRailAttrsFixture";
import { chatRowFixture } from "./chatRowFixture";
import type { ChatRow } from "./rows";

let root: HTMLElement | null = null;

afterEach(() => {
  if (root === null) return;
  m.mount(root, null);
  root.remove();
  root = null;
});

function mount(rows: readonly ChatRow[]): HTMLElement {
  const mounted = document.createElement("div");
  document.body.appendChild(mounted);
  root = mounted;
  m.mount(mounted, { view: () => m(ChatRail, chatRailAttrsFixture({ rows })) });
  return mounted;
}

function dotClassOf(chatId: string): string {
  const dot = root?.querySelector(`[data-chat-id="${chatId}"] .chat-rail-dot`);
  if (dot === null || dot === undefined) throw new Error(`no status mark for ${chatId}`);
  return dot.getAttribute("class") ?? "";
}

function chatIdsIn(element: Element): string[] {
  return [...element.querySelectorAll<HTMLElement>(".chat-rail-row")].map((rowElement) => rowElement.dataset.chatId!);
}

describe("ChatRail status marks", () => {
  it("draws background as the dashed ring, apart from working and idle", () => {
    const rail = mount([
      chatRowFixture("agent-wait", { status: "background" }),
      chatRowFixture("agent-turn", { status: "working" }),
      chatRowFixture("agent-idle"),
    ]);

    expect(dotClassOf("agent-wait")).toContain("chat-rail-dot--dashed");
    expect(dotClassOf("agent-wait")).not.toContain("chat-rail-dot--pulse");
    expect(dotClassOf("agent-turn")).toContain("chat-rail-dot--pulse");
    expect(dotClassOf("agent-turn")).not.toContain("chat-rail-dot--dashed");
    expect(dotClassOf("agent-idle")).not.toMatch(/chat-rail-dot--(dashed|pulse)/);
    expect(rail.querySelector('[data-chat-id="agent-wait"]')?.getAttribute("data-status")).toBe("background");
  });

  it("leaves idle and stopped chats an empty slot, and dims only the stopped row", () => {
    const rail = mount([chatRowFixture("agent-idle"), chatRowFixture("agent-stopped", { status: "stopped" })]);

    for (const chatId of ["agent-idle", "agent-stopped"]) {
      const dot = rail.querySelector(`[data-chat-id="${chatId}"] .chat-rail-dot`)!;
      expect(dot.tagName).toBe("SPAN");
      expect(dot.childElementCount).toBe(0);
      expect(dotClassOf(chatId)).not.toMatch(/\b(bg-|border)/);
    }
    expect(rail.querySelector('[data-chat-id="agent-stopped"]')?.className).toContain("opacity-50");
    expect(rail.querySelector('[data-chat-id="agent-idle"]')?.className).not.toContain("opacity-50");
  });
});

describe("ChatRail participant photos", () => {
  afterEach(() => {
    setParticipantsForTesting(null, []);
  });

  it("shows who has written in a chat only when two or more have, stepping aside under the pointer", () => {
    const rail = mount([
      chatRowFixture("agent-shared", { participants: ["user:josh-1", "user:bob-2"] }),
      chatRowFixture("agent-solo", { participants: ["user:josh-1"] }),
      chatRowFixture("agent-quiet"),
    ]);
    const photos = rail.querySelector('[data-chat-row-people="agent-shared"]');
    expect(photos).not.toBeNull();
    expect(photos?.querySelectorAll("[data-participant-picture]")).toHaveLength(2);
    // Out of the way on hover, where the pencil and the kebab come out.
    expect(photos?.className).toContain("group-hover:hidden");
    expect(rail.querySelector('[data-chat-row-people="agent-solo"]')).toBeNull();
    expect(rail.querySelector('[data-chat-row-people="agent-quiet"]')).toBeNull();
  });

  it("folds the owner's two keys into one owner before counting and drawing", () => {
    setParticipantsForTesting(null, [
      {
        key: "user:josh-1",
        kind: "person",
        display_name: "Josh",
        profile_picture_url: "https://p.example/josh.png",
        email: null,
        handle: "Josh",
        color: "#000",
        is_owner: true,
        is_connected: true,
        last_seen: null,
        last_input_at: null,
      },
    ]);
    const rail = mount([
      chatRowFixture("agent-solo-owner", { participants: ["owner", "user:josh-1"] }),
      chatRowFixture("agent-with-visitor", { participants: ["owner", "user:bob-2"] }),
    ]);
    // The owner annotated before the workspace was shared and after is one person, never a stack of two.
    expect(rail.querySelector('[data-chat-row-people="agent-solo-owner"]')).toBeNull();
    // Folded, the pre-share key draws the owner's picture.
    const photos = rail.querySelector('[data-chat-row-people="agent-with-visitor"]');
    expect(photos?.querySelectorAll("[data-participant-picture]")).toHaveLength(2);
    expect(photos?.querySelector("img")?.getAttribute("src")).toBe("https://p.example/josh.png");
  });
});

describe("ChatRail main chat", () => {
  it("sets the main chat alone above the rest, its helpers right under the rule, and marks its row", () => {
    const helper = chatRowFixture("agent-helper", { labels: { agent_created: "true", lead_agent: "agent-welcome" } });
    const rail = mount([chatRowFixture("agent-welcome", { isMain: true }), helper, chatRowFixture("agent-other")]);

    const group = rail.querySelector(".chat-rail-main-group");
    expect(group).not.toBeNull();
    expect(chatIdsIn(group!)).toEqual(["agent-welcome"]);
    expect(chatIdsIn(rail)).toEqual(["agent-welcome", "agent-helper", "agent-other"]);
    expect(rail.querySelector('[data-chat-id="agent-welcome"]')?.getAttribute("data-main")).toBe("true");
    expect(rail.querySelector('[data-chat-id="agent-other"]')?.hasAttribute("data-main")).toBe(false);
  });

  it("draws no main group when no chat is the main chat", () => {
    const rail = mount([chatRowFixture("agent-a"), chatRowFixture("agent-b")]);

    expect(rail.querySelector(".chat-rail-main-group")).toBeNull();
    expect(chatIdsIn(rail)).toEqual(["agent-a", "agent-b"]);
  });

  it("offers to make a chat the main chat, except the main chat itself and a helper another agent started", () => {
    const welcome = chatRowFixture("agent-welcome", { isMain: true });
    const other = chatRowFixture("agent-other");
    const helper = chatRowFixture("agent-helper", { labels: { agent_created: "true", lead_agent: "agent-welcome" } });
    const context = chatRailAttrsFixture({ rows: [welcome, helper, other] });
    const labelsOf = (row: ChatRow): string[] =>
      rowMenuRows(context, row).flatMap((menuRow) => (menuRow.kind === "action" ? [menuRow.label] : []));

    expect(labelsOf(welcome)).not.toContain("Make main chat");
    expect(labelsOf(helper)).not.toContain("Make main chat");
    const makeMain = rowMenuRows(context, other).find(
      (menuRow) => menuRow.kind === "action" && menuRow.key === "make-main",
    );
    if (makeMain?.kind !== "action") throw new Error("no Make main chat row");
    makeMain.onSelect();

    expect(verbs.makeMainChat).toHaveBeenCalledWith("agent-other");
  });

  it("offers no stop or restart on the main chat, which is kept running", () => {
    const main = chatRowFixture("agent-main", { isMain: true });
    const stoppedMain = chatRowFixture("agent-main", { isMain: true, status: "stopped" });
    const other = chatRowFixture("agent-other");
    const context = chatRailAttrsFixture({ rows: [main, other] });
    const keysOf = (row: ChatRow): string[] =>
      rowMenuRows(context, row).flatMap((menuRow) =>
        menuRow.kind === "action" && menuRow.key !== undefined ? [menuRow.key] : [],
      );

    expect(keysOf(main)).not.toContain("stop");
    expect(keysOf(stoppedMain)).not.toContain("start");
    expect(keysOf(other)).toContain("stop");
  });

  it("gives a chat waiting for its first send its verbs, and a chat being created its rename and delete too", () => {
    const waiting = chatRowFixture("agent-waiting", { kind: "provisional", status: "idle" });
    const creating = chatRowFixture("agent-creating", { kind: "provisional", status: "working" });
    const context = chatRailAttrsFixture({ rows: [waiting, creating] });
    const rail = mount([waiting, creating]);

    expect(
      rowMenuRows(context, waiting).flatMap((menuRow) =>
        menuRow.kind === "action" && menuRow.key !== undefined ? [menuRow.key] : [],
      ),
    ).toEqual(["rename", "make-main", "delete"]);
    expect(
      rowMenuRows(context, creating).flatMap((menuRow) =>
        menuRow.kind === "action" && menuRow.key !== undefined ? [menuRow.key] : [],
      ),
    ).toEqual(["rename", "make-main", "delete"]);
    expect(rail.querySelector('[data-chat-id="agent-waiting"] .chat-rail-rename')).not.toBeNull();
    expect(rail.querySelector('[data-chat-id="agent-creating"] .chat-rail-rename')).not.toBeNull();
  });
});

describe("ChatRail rooms", () => {
  it("draws a room with the people glyph and its mention mark, and offers it rename, stop or restart, and delete", () => {
    const lounge = chatRowFixture("room-0123456789abcdef", { kind: "room", title: "Lounge", hasMention: true });
    const busy = chatRowFixture("room-fedcba9876543210", { kind: "room", title: "Hall", status: "working" });
    const stopped = chatRowFixture("room-0000000000000001", { kind: "room", title: "Attic", status: "stopped" });
    const rail = mount([lounge, busy]);

    expect(rail.querySelector('[data-chat-id="room-0123456789abcdef"] .chat-rail-room-glyph')).not.toBeNull();
    expect(rail.querySelector('[data-chat-id="room-0123456789abcdef"] .chat-rail-dot')).toBeNull();
    expect(rail.querySelector('[data-chat-id="room-0123456789abcdef"] [data-chat-row-mention]')).not.toBeNull();
    // While an agent the room asked works, the room wears the working dot, as any chat does.
    expect(dotClassOf("room-fedcba9876543210")).toContain("chat-rail-dot--pulse");
    expect(rail.querySelector('[data-chat-id="room-fedcba9876543210"] [data-chat-row-mention]')).toBeNull();
    const context = chatRailAttrsFixture({ rows: [lounge, busy, stopped] });
    const labelsOf = (row: ChatRow): string[] =>
      rowMenuRows(context, row).map((menuRow) => (menuRow.kind === "action" ? menuRow.label : "-"));
    // A room's reader is stopped and restarted from the menu like any chat's agent, and never made the main chat.
    expect(labelsOf(lounge)).toEqual(["Rename", "Stop room", "-", "Delete room"]);
    expect(labelsOf(stopped)).toEqual(["Rename", "Restart room", "-", "Delete room"]);
  });
});

describe("ChatRail right-click", () => {
  let uninstall: (() => void) | null = null;
  afterEach(() => {
    uninstall?.();
    uninstall = null;
  });

  /** The rail over two chats, under the root's element menu wired as the root wires it. */
  function mountUnderElementMenu(isDrawnByHost: boolean): { openElsewhere: ReturnType<typeof vi.fn> } {
    const attrs = chatRailAttrsFixture({ rows: [chatRowFixture("agent-1"), chatRowFixture("agent-2")] });
    const mounted = document.createElement("div");
    document.body.appendChild(mounted);
    root = mounted;
    m.mount(mounted, { view: () => m(ChatRail, attrs) });
    const openElsewhere = vi.fn();
    uninstall = installElementContextMenu({
      draft: () => undefined,
      isDraftAvailable: () => true,
      openLink: () => undefined,
      isDrawnByHost: () => isDrawnByHost,
      ...railContextMenuRoutes(
        () => attrs,
        () => isDrawnByHost,
        openElsewhere,
      ),
    });
    return { openElsewhere };
  }

  function rightClick(element: Element): MouseEvent {
    const event = new MouseEvent("contextmenu", { bubbles: true, cancelable: true, clientX: 20, clientY: 30 });
    element.dispatchEvent(event);
    m.redraw.sync();
    return event;
  }

  it("opens the row's own menu, its verbs then the element menu's rows, and leaves the rest of the rail elsewhere", () => {
    const { openElsewhere } = mountUnderElementMenu(false);
    expect(rightClick(root!.querySelector('[data-chat-id="agent-2"]')!).defaultPrevented).toBe(true);
    const keys = [...document.querySelectorAll(".chat-rail-menu [data-menu-row]")].map((row) =>
      row.getAttribute("data-menu-row"),
    );
    expect(keys.slice(0, 2)).toEqual(["rename", "make-main"]);
    expect(keys).toContain("delete");
    expect(keys.slice(-3)).toEqual(["copy-reference", "explain-element", "modify-element"]);
    expect(openElsewhere).not.toHaveBeenCalled();
    rightClick(root!.querySelector(".chat-rail")!);
    expect(openElsewhere).toHaveBeenCalledTimes(1);
  });

  it("where the desktop app draws the menu, hands it the row's verbs and runs the one it picks", async () => {
    mountUnderElementMenu(true);
    vi.spyOn(document, "hasFocus").mockReturnValue(true);
    expect(rightClick(root!.querySelector('[data-chat-id="agent-2"]')!).defaultPrevented).toBe(false);
    expect(document.querySelector(".chat-rail-menu")).toBeNull();
    const bridge = Reflect.get(window, HOST_MENU_GLOBAL) as HostMenuBridge;
    const page = bridge.sections()!.page;
    expect(page.map((item) => (item.kind === "action" ? item.key : item.kind))).toEqual([
      "rename",
      "make-main",
      "stop",
      "divider",
      "delete",
    ]);
    expect(page[page.length - 1]).toMatchObject({ symbol: "trash" });
    await bridge.select("make-main");
    expect(verbs.makeMainChat).toHaveBeenCalledWith("agent-2");
  });
});

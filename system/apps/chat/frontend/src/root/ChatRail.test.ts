// @vitest-environment jsdom
/**
 * The rail's status marks: a chat waiting on a background task wears the dashed ring, a working one the breathing
 * dot (pending tasks or not, since the backend reports it working), and an idle one the hollow ring. And the
 * main chat: it leads in its own group, set off from the rest, and its row is marked. And the delete: a confirmed
 * chat leaves the list before the chat app answers, and comes back with the reason in a notice on a refusal. And a
 * refused rename: a notice says why, and waits while another chat's name is being typed.
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
import { HttpError } from "@imbue/workspace-ui/src/models/http";
import { setParticipantsForTesting } from "../models/Participants";
import { ChatRail, endRenameOfUnlisted, rowMenuRows } from "./ChatRail";
import type { ChatRailAttrs, RowMenuContext } from "./ChatRail";
import { chatRailAttrsFixture } from "./chatRailAttrsFixture";
import { chatRowFixture } from "./chatRowFixture";
import { hasPendingDelete, settlePendingDeletes, withoutPendingDeletes } from "./pendingDeletes";
import { RootNoticeView, isRootNoticeUp } from "./rootNotice";
import type { ChatRow } from "./rows";

let root: HTMLElement | null = null;

afterEach(() => {
  settlePendingDeletes(new Set());
  vi.restoreAllMocks();
  // A rename left open holds the notices, which the loop below could not then see.
  endRenameOfUnlisted(new Set());
  while (isRootNoticeUp()) dismissNotice();
  if (root === null) return;
  m.mount(root, null);
  root.remove();
  root = null;
});

/** Mount the list over ``rows``, or over what ``rows`` answers at each draw for a test whose list changes. */
function mount(
  rows: readonly ChatRow[] | (() => readonly ChatRow[]),
  overrides: Partial<ChatRailAttrs> = {},
): HTMLElement {
  const rowsNow = typeof rows === "function" ? rows : () => rows;
  const mounted = document.createElement("div");
  document.body.appendChild(mounted);
  root = mounted;
  m.mount(mounted, {
    view: () => [m(ChatRail, chatRailAttrsFixture({ ...overrides, rows: rowsNow() })), m(RootNoticeView)],
  });
  return mounted;
}

function noticeText(): string | null {
  return document.querySelector(".modal-card")?.textContent ?? null;
}

function noticeButton(label: string): HTMLButtonElement {
  const button = [...document.querySelectorAll<HTMLButtonElement>(".modal-card button")].find(
    (candidate) => candidate.textContent === label,
  );
  if (button === undefined) throw new Error(`no "${label}" button on the notice`);
  return button;
}

function dismissNotice(): void {
  if (root === null) {
    // Nothing draws the notice: mount a bare view to press its OK.
    const mounted = document.createElement("div");
    document.body.appendChild(mounted);
    m.mount(mounted, RootNoticeView);
    document.querySelector<HTMLButtonElement>(".notice-dismiss")!.click();
    m.mount(mounted, null);
    mounted.remove();
    return;
  }
  document.querySelector<HTMLButtonElement>(".notice-dismiss")!.click();
  m.redraw.sync();
}

function deleteFromMenu(context: RowMenuContext, row: ChatRow): void {
  const deleteRow = rowMenuRows(context, row).find((menuRow) => menuRow.kind === "action" && menuRow.key === "delete");
  if (deleteRow?.kind !== "action") throw new Error("no Delete chat row");
  deleteRow.onSelect();
}

/** Hold the next ``count`` renames unanswered; the refusers returned refuse them, in order. */
function holdRenames(count: number): ((error: unknown) => void)[] {
  const refusals: ((error: unknown) => void)[] = [];
  for (let index = 0; index < count; index += 1) {
    verbs.renameChat.mockReturnValueOnce(
      new Promise<undefined>((_resolve, reject) => {
        refusals.push(reject);
      }),
    );
  }
  return refusals;
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

describe("ChatRail delete", () => {
  const first = chatRowFixture("agent-first");
  const second = chatRowFixture("agent-second");

  it("takes a confirmed chat off the list before the chat app answers, and moves off it when it is on screen", () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    verbs.destroyChat.mockReturnValueOnce(new Promise<undefined>(() => undefined));
    const onPick = vi.fn<(chatId: string | null) => void>();

    deleteFromMenu(chatRailAttrsFixture({ rows: [first, second], selectedChatId: first.chatId, onPick }), first);

    expect(withoutPendingDeletes([first, second])).toEqual([second]);
    expect(onPick).toHaveBeenCalledWith(second.chatId);
    expect(verbs.destroyChat).toHaveBeenCalledWith(first.chatId);
  });

  it("stays on the chat on screen when another one is deleted", () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    verbs.destroyChat.mockReturnValueOnce(new Promise<undefined>(() => undefined));
    const onPick = vi.fn<(chatId: string | null) => void>();

    deleteFromMenu(chatRailAttrsFixture({ rows: [first, second], selectedChatId: first.chatId, onPick }), second);

    expect(withoutPendingDeletes([first, second])).toEqual([first]);
    expect(onPick).not.toHaveBeenCalled();
  });

  it("does nothing when the question is declined", () => {
    vi.spyOn(window, "confirm").mockReturnValue(false);
    verbs.destroyChat.mockClear();

    deleteFromMenu(chatRailAttrsFixture({ rows: [first], selectedChatId: first.chatId }), first);

    expect(hasPendingDelete()).toBe(false);
    expect(verbs.destroyChat).not.toHaveBeenCalled();
  });

  it("brings the chat back and says why in a notice when the chat app refuses, and tries again from it", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    mount([first, second]);
    verbs.destroyChat.mockClear();
    verbs.destroyChat.mockRejectedValueOnce(new HttpError(500, "mngr failed"));
    const onDeleteRefused = vi.fn<() => void>();

    deleteFromMenu(chatRailAttrsFixture({ rows: [first, second], onDeleteRefused }), first);
    expect(hasPendingDelete()).toBe(true);

    await vi.waitFor(() => expect(noticeText()).not.toBeNull());
    m.redraw.sync();
    expect(withoutPendingDeletes([first, second])).toEqual([first, second]);
    expect(onDeleteRefused).toHaveBeenCalledTimes(1);
    expect(noticeText()).toContain("Couldn't delete the chat");
    expect(noticeText()).toContain(`"${first.title}" was not deleted.`);
    expect(noticeText()).toContain("mngr failed");

    verbs.destroyChat.mockReturnValueOnce(new Promise<undefined>(() => undefined));
    noticeButton("Try again").click();
    expect(isRootNoticeUp()).toBe(false);
    expect(verbs.destroyChat).toHaveBeenCalledTimes(2);
    expect(withoutPendingDeletes([first, second])).toEqual([second]);
  });

  it("does not try again to delete a chat that has left the list since", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    let rows: readonly ChatRow[] = [first, second];
    mount(() => rows);
    verbs.destroyChat.mockClear();
    verbs.destroyChat.mockRejectedValueOnce(new TypeError("Failed to fetch"));

    deleteFromMenu(chatRailAttrsFixture({ rows: [first, second] }), first);
    await vi.waitFor(() => expect(noticeText()).not.toBeNull());
    rows = [second];
    m.redraw.sync();

    noticeButton("Try again").click();
    m.redraw.sync();
    expect(isRootNoticeUp()).toBe(false);
    expect(verbs.destroyChat).toHaveBeenCalledTimes(1);
  });

  it("says it is not known whether the chat was deleted when nothing answered", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    mount([first, second]);
    verbs.destroyChat.mockRejectedValueOnce(new TypeError("Failed to fetch"));

    deleteFromMenu(chatRailAttrsFixture({ rows: [first, second] }), first);

    await vi.waitFor(() => expect(noticeText()).not.toBeNull());
    expect(noticeText()).toContain(
      `The workspace didn't answer, so it isn't known whether "${first.title}" was deleted.`,
    );
    expect(noticeText()).not.toContain("Failed to fetch");
  });
});

describe("ChatRail refused rename", () => {
  const first = chatRowFixture("agent-first");
  const second = chatRowFixture("agent-second");

  function renameInPlace(chatId: string, title: string, key: "Enter" | null = "Enter"): HTMLInputElement {
    root!
      .querySelector<HTMLElement>(`.chat-rail-row[data-chat-id="${chatId}"]`)!
      .dispatchEvent(new MouseEvent("dblclick", { bubbles: true }));
    m.redraw.sync();
    const field = root!.querySelector<HTMLInputElement>(".chat-rail-rename-input")!;
    field.value = title;
    field.dispatchEvent(new Event("input", { bubbles: true }));
    if (key !== null) field.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true }));
    m.redraw.sync();
    return field;
  }

  it("says why in a notice, whose Rename again reopens the field on the name typed", async () => {
    mount([first, second]);
    verbs.renameChat.mockRejectedValueOnce(
      new HttpError(409, "A chat named 'Taken' already exists; pick another name"),
    );

    renameInPlace(first.chatId, "Taken");
    expect(root!.querySelector(".chat-rail-rename-input")).toBeNull();

    await vi.waitFor(() => expect(noticeText()).not.toBeNull());
    expect(noticeText()).toContain("Couldn't rename the chat");
    expect(noticeText()).toContain(`"${first.title}" was not renamed to "Taken".`);
    expect(noticeText()).toContain("A chat named 'Taken' already exists; pick another name");

    noticeButton("Rename again").click();
    m.redraw.sync();
    expect(isRootNoticeUp()).toBe(false);
    const field = root!.querySelector<HTMLInputElement>(`[data-chat-id="${first.chatId}"] .chat-rail-rename-input`);
    expect(field?.value).toBe("Taken");
  });

  it("holds the notice while another chat's name is being typed, and shows it when that field closes", async () => {
    mount([first, second]);
    const [refuse] = holdRenames(1);

    renameInPlace(first.chatId, "Taken");
    const field = renameInPlace(second.chatId, "Half typ", null);
    refuse(new HttpError(409, "the chat is moving to another agent"));
    await Promise.resolve();
    await Promise.resolve();
    m.redraw.sync();

    expect(isRootNoticeUp()).toBe(false);
    expect(root!.querySelector(".chat-rail-rename-input")).toBe(field);

    field.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    m.redraw.sync();
    expect(noticeText()).toContain(`"${first.title}" was not renamed to "Taken".`);
    expect(noticeText()).toContain("the chat is moving to another agent");
  });

  it("holds a refused delete's notice while a chat's name is being typed, so the half-typed name is not saved", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    mount([first, second]);
    verbs.renameChat.mockClear();
    verbs.destroyChat.mockRejectedValueOnce(new HttpError(500, "mngr failed"));

    const field = renameInPlace(second.chatId, "Half typ", null);
    deleteFromMenu(chatRailAttrsFixture({ rows: [first, second] }), first);
    await vi.waitFor(() => expect(withoutPendingDeletes([first, second])).toEqual([first, second]));
    m.redraw.sync();

    expect(isRootNoticeUp()).toBe(false);
    expect(root!.querySelector(".chat-rail-rename-input")).toBe(field);
    expect(verbs.renameChat).not.toHaveBeenCalled();

    field.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    m.redraw.sync();
    expect(noticeText()).toContain(`"${first.title}" was not deleted.`);
  });

  it("lets held notices show once the chat being renamed leaves the list", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    mount([first, second]);
    verbs.destroyChat.mockRejectedValueOnce(new HttpError(500, "mngr failed"));

    renameInPlace(second.chatId, "Half typ", null);
    deleteFromMenu(chatRailAttrsFixture({ rows: [first, second] }), first);
    await vi.waitFor(() => expect(withoutPendingDeletes([first, second])).toEqual([first, second]));
    expect(isRootNoticeUp()).toBe(false);

    endRenameOfUnlisted(new Set([first.chatId]));
    m.redraw.sync();
    expect(noticeText()).toContain(`"${first.title}" was not deleted.`);
  });

  it("keeps the next notice back while Rename again's field is open", async () => {
    mount([first, second]);
    const refusals = holdRenames(2);

    renameInPlace(first.chatId, "Taken");
    renameInPlace(second.chatId, "Also taken");
    refusals.forEach((refuse) => refuse(new HttpError(409, "the name is taken")));
    await vi.waitFor(() => expect(noticeText()).not.toBeNull());
    m.redraw.sync();
    verbs.renameChat.mockClear();

    noticeButton("Rename again").click();
    m.redraw.sync();
    const field = root!.querySelector<HTMLInputElement>(`[data-chat-id="${first.chatId}"] .chat-rail-rename-input`);
    expect(field?.value).toBe("Taken");
    expect(isRootNoticeUp()).toBe(false);
    expect(verbs.renameChat).not.toHaveBeenCalled();

    field!.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    m.redraw.sync();
    expect(noticeText()).toContain(`"${second.title}" was not renamed to "Also taken".`);
  });

  it("closes the notice without holding the next one when Rename again's chat has left the list", async () => {
    let rows: readonly ChatRow[] = [first, second];
    mount(() => rows);
    const refusals = holdRenames(2);

    renameInPlace(first.chatId, "Taken");
    renameInPlace(second.chatId, "Also taken");
    refusals.forEach((refuse) => refuse(new HttpError(409, "the name is taken")));
    await vi.waitFor(() => expect(noticeText()).not.toBeNull());
    rows = [second];
    m.redraw.sync();

    noticeButton("Rename again").click();
    m.redraw.sync();
    expect(root!.querySelector(".chat-rail-rename-input")).toBeNull();
    expect(noticeText()).toContain(`"${second.title}" was not renamed to "Also taken".`);
  });

  it("shows the chat on a phone before Rename again opens its field, since only the header can draw it", async () => {
    const onPick = vi.fn<(chatId: string | null) => void>();
    mount([first, second], { isInDrawer: true, selectedChatId: second.chatId, onPick });
    verbs.renameChat.mockRejectedValueOnce(new HttpError(409, "the name is taken"));

    renameInPlace(first.chatId, "Taken");
    await vi.waitFor(() => expect(noticeText()).not.toBeNull());
    noticeButton("Rename again").click();

    expect(onPick).toHaveBeenCalledWith(first.chatId);
  });

  it("leaves the chat on screen when Rename again's chat is already shown", async () => {
    const onPick = vi.fn<(chatId: string | null) => void>();
    mount([first, second], { isInDrawer: true, selectedChatId: first.chatId, onPick });
    verbs.renameChat.mockRejectedValueOnce(new HttpError(409, "the name is taken"));

    renameInPlace(first.chatId, "Taken");
    await vi.waitFor(() => expect(noticeText()).not.toBeNull());
    onPick.mockClear();
    noticeButton("Rename again").click();
    m.redraw.sync();

    expect(onPick).not.toHaveBeenCalled();
    expect(root!.querySelector<HTMLInputElement>(".chat-rail-rename-input")?.value).toBe("Taken");
  });

  it("says it is not known whether the chat was renamed when nothing answered", async () => {
    mount([first, second]);
    verbs.renameChat.mockRejectedValueOnce(new TypeError("Failed to fetch"));

    renameInPlace(first.chatId, "New name");

    await vi.waitFor(() => expect(noticeText()).not.toBeNull());
    expect(noticeText()).toContain(
      `The workspace didn't answer, so it isn't known whether "${first.title}" was renamed to "New name".`,
    );
  });

  it("names a room as a room when its rename or delete is refused", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const lounge = chatRowFixture("room-0123456789abcdef", { kind: "room", title: "Lounge" });
    mount([lounge, first]);
    verbs.renameChat.mockRejectedValueOnce(new HttpError(409, "the name is taken"));

    renameInPlace(lounge.chatId, "Hall");
    await vi.waitFor(() => expect(noticeText()).not.toBeNull());
    expect(noticeText()).toContain("Couldn't rename the room");
    dismissNotice();

    verbs.destroyChat.mockRejectedValueOnce(new HttpError(500, "mngr failed"));
    deleteFromMenu(chatRailAttrsFixture({ rows: [lounge, first] }), lounge);
    await vi.waitFor(() => expect(noticeText()).not.toBeNull());
    expect(noticeText()).toContain("Couldn't delete the room");
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

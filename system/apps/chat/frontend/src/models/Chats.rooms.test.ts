import { describe, expect, it, vi } from "vitest";

vi.mock("mithril", () => ({ default: { request: vi.fn(), redraw: vi.fn() } }));
vi.mock("../document-meta", () => ({ getChatKind: () => "agent" }));

import { getRoomById, getRooms, isRoomChat, setRoomsForTesting, whenRoomListed } from "./Chats";

const LOUNGE = {
  id: "room-0123456789abcdef",
  kind: "room" as const,
  title: "Lounge",
  last_messaged_at: null,
  awaiting_chat_ids: [],
};

describe("rooms on the chats model", () => {
  it("lists the rooms it was given and tells a room's page from an agent's by them", () => {
    setRoomsForTesting([LOUNGE]);

    expect(getRooms()).toEqual([LOUNGE]);
    expect(getRoomById("room-0123456789abcdef")).toEqual(LOUNGE);
    expect(isRoomChat("room-0123456789abcdef")).toBe(true);
    expect(isRoomChat("agent-1")).toBe(false);
  });

  it("resolves a wait for a listed room at once, and a wait for an unlisted one at its timeout", async () => {
    setRoomsForTesting([LOUNGE]);
    await expect(whenRoomListed("room-0123456789abcdef")).resolves.toBeUndefined();
    await expect(whenRoomListed("room-fedcba9876543210", 10)).resolves.toBeUndefined();
  });
});

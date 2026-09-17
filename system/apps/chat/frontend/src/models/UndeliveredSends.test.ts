import { describe, expect, it, vi } from "vitest";

import { UndeliveredSendAbsorber } from "./UndeliveredSends";
import { chatSnapshotFixture } from "./chatSnapshotFixture";

vi.mock("mithril", () => ({ default: { redraw: () => undefined } }));
// The real one writes localStorage and redraws; the absorber's job is deciding what to hand it.
vi.mock("../views/MessageInput", () => ({ prependToComposer: () => undefined }));

function snapshotHolding(chatId: string, sends: { message_id: string; text: string }[]) {
  return chatSnapshotFixture(chatId, { undelivered_sends: sends });
}

function makeAbsorber() {
  const prepended: [string, string][] = [];
  const taken: [string, string][] = [];
  const absorber = new UndeliveredSendAbsorber(
    (chatId, block) => prepended.push([chatId, block]),
    async (chatId, messageId) => {
      taken.push([chatId, messageId]);
    },
  );
  return { absorber, prepended, taken };
}

describe("UndeliveredSendAbsorber", () => {
  it("puts a send the agent refused back in its chat's composer, and says it has it", () => {
    const { absorber, prepended, taken } = makeAbsorber();

    absorber.absorb([snapshotHolding("chat-1", [{ message_id: "m-1", text: "the thing I typed" }])]);

    expect(prepended).toEqual([["chat-1", "the thing I typed"]]);
    expect(taken).toEqual([["chat-1", "m-1"]]);
  });

  it("does not paste the same send twice when the snapshot carries it again", () => {
    // The backend keeps offering it until the take lands, so a take that failed (or a snapshot
    // that raced it) re-delivers one the composer already has. Pasting it twice would put two
    // copies of the user's message in the box.
    const { absorber, prepended } = makeAbsorber();
    const snapshot = snapshotHolding("chat-1", [{ message_id: "m-1", text: "the thing I typed" }]);

    absorber.absorb([snapshot]);
    absorber.absorb([snapshot]);

    expect(prepended).toEqual([["chat-1", "the thing I typed"]]);
  });

  it("keeps each chat's sends in its own composer, in order", () => {
    const { absorber, prepended } = makeAbsorber();

    absorber.absorb([
      snapshotHolding("chat-1", [
        { message_id: "m-1", text: "first" },
        { message_id: "m-2", text: "second" },
      ]),
      snapshotHolding("chat-2", [{ message_id: "m-3", text: "elsewhere" }]),
    ]);

    expect(prepended).toEqual([
      ["chat-1", "first"],
      ["chat-1", "second"],
      ["chat-2", "elsewhere"],
    ]);
  });

  it("does nothing for chats holding none", () => {
    const { absorber, prepended, taken } = makeAbsorber();

    absorber.absorb([chatSnapshotFixture("chat-1")]);

    expect(prepended).toEqual([]);
    expect(taken).toEqual([]);
  });
});

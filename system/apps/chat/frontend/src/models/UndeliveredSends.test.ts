import { describe, expect, it, vi } from "vitest";

import { UndeliveredSendAbsorber } from "./UndeliveredSends";
import { chatSnapshotFixture } from "./chatSnapshotFixture";

// The real one writes localStorage and redraws; the absorber's job is deciding what to hand it.
vi.mock("../views/MessageInput", () => ({ prependToComposer: () => undefined, raiseFailureNotice: () => undefined }));

function held(message_id: string, text: string, detail = "You've hit your session limit") {
  return { message_id, text, detail, kind: "rejected_by_agent" };
}

function snapshotHolding(chatId: string, sends: ReturnType<typeof held>[]) {
  return chatSnapshotFixture(chatId, { undelivered_sends: sends });
}

const OWN_CHAT = "chat-1";

function makeAbsorber(ownChatId: string = OWN_CHAT) {
  const prepended: [string, string][] = [];
  const taken: [string, string][] = [];
  const raised: [string, string][] = [];
  const absorber = new UndeliveredSendAbsorber(
    ownChatId,
    (chatId, block) => prepended.push([chatId, block]),
    async (chatId, messageId) => {
      taken.push([chatId, messageId]);
    },
    (chatId, detail) => raised.push([chatId, detail]),
  );
  return { absorber, prepended, taken, raised };
}

describe("UndeliveredSendAbsorber", () => {
  it("puts a send the agent refused back in its chat's composer, and says it has it", () => {
    const { absorber, prepended, taken } = makeAbsorber();

    absorber.absorb([snapshotHolding("chat-1", [held("m-1", "the thing I typed")])]);

    expect(prepended).toEqual([["chat-1", "the thing I typed"]]);
    expect(taken).toEqual([["chat-1", "m-1"]]);
  });

  it("does not paste the same send twice when the snapshot carries it again", () => {
    // The backend keeps offering it until the take lands, so a take that failed (or a snapshot
    // that raced it) re-delivers one the composer already has. Pasting it twice would put two
    // copies of the user's message in the box.
    const { absorber, prepended } = makeAbsorber();
    const snapshot = snapshotHolding("chat-1", [held("m-1", "the thing I typed")]);

    absorber.absorb([snapshot]);
    absorber.absorb([snapshot]);

    expect(prepended).toEqual([["chat-1", "the thing I typed"]]);
  });

  it("takes only this page's own chat's sends, in order", () => {
    const { absorber, prepended } = makeAbsorber();

    absorber.absorb([
      snapshotHolding(OWN_CHAT, [held("m-1", "first"), held("m-2", "second")]),
      snapshotHolding("chat-2", [held("m-3", "elsewhere")]),
    ]);

    // The other chat's send is left for the page that owns it.
    expect(prepended).toEqual([
      [OWN_CHAT, "first"],
      [OWN_CHAT, "second"],
    ]);
  });

  it("re-issues the take for a send it has already pasted", () => {
    // The take is what clears the send from the record. If the first one is lost and the paste
    // guard also skipped the take, the send would ride every snapshot forever and (for a chat
    // on its first agent) hold its record open with it.
    const { absorber, taken } = makeAbsorber();
    const snapshot = snapshotHolding(OWN_CHAT, [held("m-1", "the thing I typed")]);

    absorber.absorb([snapshot]);
    absorber.absorb([snapshot]);

    expect(taken).toEqual([
      [OWN_CHAT, "m-1"],
      [OWN_CHAT, "m-1"],
    ]);
  });

  it("says why the message came back, in the harness's words", () => {
    // Handing the text back with nothing said invites pressing send again, which on the
    // commonest cause of this earns the same refusal.
    const { absorber, raised } = makeAbsorber();

    absorber.absorb([snapshotHolding(OWN_CHAT, [held("m-1", "the thing I typed")])]);

    expect(raised).toEqual([[OWN_CHAT, "You've hit your session limit"]]);
  });

  it("does nothing for chats holding none", () => {
    const { absorber, prepended, taken } = makeAbsorber();

    absorber.absorb([chatSnapshotFixture("chat-1")]);

    expect(prepended).toEqual([]);
    expect(taken).toEqual([]);
  });
});

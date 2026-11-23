import { describe, expect, it } from "vitest";
import { BUILD_APP_COACH_TEXT, CONNECT_DATA_COACH_TEXT } from "@imbue/workspace-ui/src/onboarding_pointers";
import { SHOW_NEW_CHAT_POINTER_MESSAGE, isPointerAvailable, showPointer } from "./pointers";
import type { PointerChannels } from "./pointers";

/** Channels that record what goes out through each. */
function recordingChannels(): { channels: PointerChannels; sent: unknown[] } {
  const sent: unknown[] = [];
  return {
    sent,
    channels: {
      sendMessage: (type, fields) => sent.push({ channel: "message", type, fields }),
      showCoachCard: (text, place) => sent.push({ channel: "coach-card", text, place }),
      openShareSettings: (coachMark) => sent.push({ channel: "share-settings", coachMark }),
    },
  };
}

describe("what a click on a checklist entry asks for", () => {
  it("is a message for the app that draws the New chat pointer, for starting a new chat", () => {
    const { channels, sent } = recordingChannels();

    showPointer("new_chat", channels);

    expect(sent).toEqual([{ channel: "message", type: "onboarding:show-new-chat-pointer", fields: {} }]);
    expect(SHOW_NEW_CHAT_POINTER_MESSAGE).toBe("onboarding:show-new-chat-pointer");
  });

  it("is the chrome's share panel with a bubble at its phone control, for phone access", () => {
    const { channels, sent } = recordingChannels();

    showPointer("phone_access", channels);

    expect(sent).toEqual([{ channel: "share-settings", coachMark: "mobile-access" }]);
  });

  it("is a shell card popping out of the agent's avatar, for building an app", () => {
    const { channels, sent } = recordingChannels();

    showPointer("build_app", channels);

    expect(sent).toEqual([{ channel: "coach-card", text: BUILD_APP_COACH_TEXT, place: { target: "avatar" } }]);
    expect(BUILD_APP_COACH_TEXT).toBe(
      "Provide some context on a problem you're facing and tell the agent to build an app to help with it.",
    );
  });

  it("is a shell card under the chrome's permissions button, for connecting data", () => {
    const { channels, sent } = recordingChannels();

    showPointer("connect_data", channels);

    expect(sent).toEqual([
      { channel: "coach-card", text: CONNECT_DATA_COACH_TEXT, place: { chromeAnchor: "permissions" } },
    ]);
    expect(CONNECT_DATA_COACH_TEXT).toBe(
      "Your connections live here. Ask me to connect any of these services, or any other data you want to pull in.",
    );
  });

  it("is nothing for an item this page does not know", () => {
    const { channels, sent } = recordingChannels();

    showPointer("learn_to_juggle", channels);

    expect(sent).toEqual([]);
  });
});

describe("whether an entry's pointer can be shown", () => {
  it("needs a chrome that draws coach marks for phone access and connecting data, and nothing for the others", () => {
    const withoutChrome = ["new_chat", "phone_access", "build_app", "connect_data"].map((id) =>
      isPointerAvailable(id, false),
    );
    const withChrome = ["new_chat", "phone_access", "build_app", "connect_data"].map((id) =>
      isPointerAvailable(id, true),
    );

    expect(withoutChrome).toEqual([true, false, true, false]);
    expect(withChrome).toEqual([true, true, true, true]);
    expect(isPointerAvailable("learn_to_juggle", true)).toBe(false);
  });
});

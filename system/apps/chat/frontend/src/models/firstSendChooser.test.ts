import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  // The chats the list names; a chat awaiting its first send is not among them.
  listedChatIds: new Set<string>(),
  // Each provisional chat's phase and the account it was minted for, by id.
  provisional: new Map<string, { phase: string; account_id: string }>(),
  launchesInFlight: new Set<string>(),
  launchChat: vi.fn(async (_chatId: string, _accountId: string, _message?: string, _kind?: string) => ({})),
  accountsLoaded: true,
  // The account a chat with no agent yet would start on; null when nothing is signed in.
  accountForFirstSend: null as { id: string } | null,
  // Whether the chooser is up: opening it again while it is does nothing, as the real one does.
  isChooserOpen: false,
  openProviderChooser: vi.fn(),
}));

vi.mock("./Chats", () => ({
  getChatById: (chatId: string) => (mocks.listedChatIds.has(chatId) ? { chat_id: chatId } : undefined),
  getProvisionalChat: (chatId: string) => {
    const provisional = mocks.provisional.get(chatId);
    return provisional === undefined ? undefined : { chat_id: chatId, ...provisional };
  },
  isLaunchInFlight: (chatId: string) => mocks.launchesInFlight.has(chatId),
  launchChat: (chatId: string, accountId: string, message?: string, kind?: string) =>
    mocks.launchChat(chatId, accountId, message, kind),
}));
vi.mock("./Providers", () => ({
  accountForFirstSend: () => mocks.accountForFirstSend,
  areAccountsLoaded: () => mocks.accountsLoaded,
  getSelectedAccount: () => mocks.accountForFirstSend,
  openProviderChooser: (intent: unknown) => {
    if (mocks.isChooserOpen) return;
    mocks.isChooserOpen = true;
    mocks.openProviderChooser(intent);
  },
}));

import { isAwaitingProvider, isNothingSignedIn, launchOnSignIn, makeRootFirstSendChooser } from "./firstSendChooser";
import type { RootFirstSendChooser } from "./firstSendChooser";

interface ChooserIntent {
  onSignedIn: (accountId: string) => void;
  onRoomChosen: () => void;
}

/** The intent of the chooser's only opening; the chooser keeps the first while it is up. */
function openedIntent(): ChooserIntent {
  expect(mocks.openProviderChooser).toHaveBeenCalledTimes(1);
  return mocks.openProviderChooser.mock.calls[0][0] as ChooserIntent;
}

beforeEach(() => {
  mocks.listedChatIds.clear();
  mocks.provisional.clear();
  mocks.provisional.set("agent-empty-list-4410", { phase: "awaiting_first_send", account_id: "" });
  mocks.launchesInFlight.clear();
  mocks.launchChat.mockClear();
  mocks.accountsLoaded = true;
  mocks.accountForFirstSend = null;
  mocks.isChooserOpen = false;
  mocks.openProviderChooser.mockClear();
});

/** A sign-in finishing, or the room row picked: either way the chooser goes. */
function closeChooser(): void {
  mocks.isChooserOpen = false;
}

describe("isAwaitingProvider", () => {
  it("holds for a chat awaiting its first send with nothing signed in", () => {
    expect(isAwaitingProvider("agent-empty-list-4410")).toBe(true);
  });

  it("does not hold before the account list has loaded, once there is an account, or for a chat past its first send", () => {
    mocks.accountsLoaded = false;
    expect(isAwaitingProvider("agent-empty-list-4410")).toBe(false);
    mocks.accountsLoaded = true;

    mocks.accountForFirstSend = { id: "acct-4411" };
    expect(isAwaitingProvider("agent-empty-list-4410")).toBe(false);
    mocks.accountForFirstSend = null;

    mocks.provisional.set("agent-empty-list-4410", { phase: "creating", account_id: "" });
    expect(isAwaitingProvider("agent-empty-list-4410")).toBe(false);

    mocks.listedChatIds.add("agent-listed-4412");
    expect(isAwaitingProvider("agent-listed-4412")).toBe(false);
  });
});

describe("launchOnSignIn", () => {
  it("launches a chat awaiting its first send on the new account, with no message", () => {
    launchOnSignIn("agent-empty-list-4410", "acct-4413");

    expect(mocks.launchChat).toHaveBeenCalledExactlyOnceWith("agent-empty-list-4410", "acct-4413", "", "agent");
  });

  it("launches nothing for a chat already creating, already listed, or whose launch is out", () => {
    mocks.provisional.set("agent-creating-4414", { phase: "creating", account_id: "" });
    launchOnSignIn("agent-creating-4414", "acct-4413");

    mocks.listedChatIds.add("agent-empty-list-4410");
    launchOnSignIn("agent-empty-list-4410", "acct-4413");
    mocks.listedChatIds.clear();

    mocks.launchesInFlight.add("agent-empty-list-4410");
    launchOnSignIn("agent-empty-list-4410", "acct-4413");

    expect(mocks.launchChat).not.toHaveBeenCalled();
  });
});

describe("isNothingSignedIn", () => {
  it("holds once the account list has loaded with nothing in it, and not before", () => {
    expect(isNothingSignedIn()).toBe(true);
    mocks.accountsLoaded = false;
    expect(isNothingSignedIn()).toBe(false);
    mocks.accountsLoaded = true;
    mocks.accountForFirstSend = { id: "acct-4411" };
    expect(isNothingSignedIn()).toBe(false);
  });
});

describe("makeRootFirstSendChooser", () => {
  /** The root's chooser over whatever ``shown`` says the root shows, with no intake under way. */
  function rootChooser(
    shown: () => string | null,
    makeRoom: (chatId: string | null) => void = vi.fn(),
    isIntakeUnderWay: () => boolean = () => false,
  ): RootFirstSendChooser {
    return makeRootFirstSendChooser({ shownChatId: shown, isIntakeUnderWay, makeRoom });
  }

  it("opens the chooser once for the chat on screen awaiting a provider, so a dismissal stands", () => {
    const chooser = rootChooser(() => "agent-empty-list-4410");

    chooser.afterDraw();
    closeChooser();
    chooser.afterDraw();

    expect(mocks.openProviderChooser).toHaveBeenCalledTimes(1);
  });

  it("opens nothing while the chat on screen has an account to start on, or nothing is on screen and accounts are unread", () => {
    let shown: string | null = "agent-empty-list-4410";
    mocks.accountForFirstSend = { id: "acct-4411" };
    const chooser = rootChooser(() => shown);
    chooser.afterDraw();

    shown = null;
    chooser.afterDraw();
    mocks.accountForFirstSend = null;
    mocks.accountsLoaded = false;
    chooser.afterDraw();

    expect(mocks.openProviderChooser).not.toHaveBeenCalled();
  });

  it("opens again from the chat's notice after a dismissal", () => {
    const chooser = rootChooser(() => "agent-empty-list-4410");
    chooser.afterDraw();
    closeChooser();

    chooser.open();

    expect(mocks.openProviderChooser).toHaveBeenCalledTimes(2);
  });

  it("launches the chat on screen when the sign-in lands, even when the selection moved while it ran", () => {
    let shown = "agent-empty-list-4410";
    const chooser = rootChooser(() => shown);
    chooser.afterDraw();
    const intent = openedIntent();

    // The seeded welcome chat arrives and is selected while the user signs in.
    mocks.provisional.set("agent-welcome-4415", { phase: "awaiting_first_send", account_id: "" });
    shown = "agent-welcome-4415";
    chooser.afterDraw();
    intent.onSignedIn("acct-4416");

    expect(mocks.launchChat).toHaveBeenCalledExactlyOnceWith("agent-welcome-4415", "acct-4416", "", "agent");
    expect(chooser.isLaunchPending()).toBe(false);
  });

  it("makes the chat on screen a room when the room row is picked, or a new one with none on screen", () => {
    let shown: string | null = "agent-empty-list-4410";
    const makeRoom = vi.fn();
    const chooser = rootChooser(() => shown, makeRoom);
    chooser.open();
    openedIntent().onRoomChosen();
    shown = null;
    openedIntent().onRoomChosen();

    expect(makeRoom.mock.calls).toEqual([["agent-empty-list-4410"], [null]]);
  });

  describe("with no chat on screen", () => {
    beforeEach(() => {
      mocks.provisional.clear();
    });

    it("opens the chooser once while nothing is signed in, before any chat exists", () => {
      const chooser = rootChooser(() => null);

      chooser.afterDraw();
      closeChooser();
      chooser.afterDraw();

      expect(mocks.openProviderChooser).toHaveBeenCalledTimes(1);
    });

    it("leaves the slot to an intake that is choosing the chat", () => {
      const chooser = rootChooser(
        () => null,
        vi.fn(),
        () => true,
      );

      chooser.afterDraw();

      expect(mocks.openProviderChooser).not.toHaveBeenCalled();
    });

    it("keeps the chooser and its sign-in when the welcome chat arrives, and the sign-in starts the welcome chat", () => {
      let shown: string | null = null;
      const chooser = rootChooser(() => shown);
      chooser.afterDraw();
      const intent = openedIntent();

      mocks.provisional.set("agent-welcome-4420", { phase: "awaiting_first_send", account_id: "" });
      shown = "agent-welcome-4420";
      chooser.afterDraw();
      expect(mocks.openProviderChooser).toHaveBeenCalledTimes(1);

      intent.onSignedIn("acct-4421");
      expect(mocks.launchChat).toHaveBeenCalledExactlyOnceWith("agent-welcome-4420", "acct-4421", "", "agent");
    });

    it("does not open again for the welcome chat when the user dismissed the chooser before it arrived", () => {
      let shown: string | null = null;
      const chooser = rootChooser(() => shown);
      chooser.afterDraw();
      closeChooser();

      // Selected before its record arrives, then pushed.
      shown = "agent-welcome-4422";
      chooser.afterDraw();
      mocks.provisional.set("agent-welcome-4422", { phase: "awaiting_first_send", account_id: "" });
      chooser.afterDraw();

      expect(mocks.openProviderChooser).toHaveBeenCalledTimes(1);
      // Its notice still opens it.
      chooser.open();
      expect(mocks.openProviderChooser).toHaveBeenCalledTimes(2);
    });

    it("opens for a later chat awaiting a provider once the first chat shown has claimed the opening", () => {
      let shown: string | null = null;
      const chooser = rootChooser(() => shown);
      chooser.afterDraw();
      closeChooser();
      mocks.listedChatIds.add("agent-listed-4423");
      shown = "agent-listed-4423";
      chooser.afterDraw();

      mocks.provisional.set("agent-new-4424", { phase: "awaiting_first_send", account_id: "" });
      shown = "agent-new-4424";
      chooser.afterDraw();

      expect(mocks.openProviderChooser).toHaveBeenCalledTimes(2);
    });

    it("holds a sign-in that lands with no chat on screen and starts the first waiting chat shown after it", () => {
      let shown: string | null = null;
      const chooser = rootChooser(() => shown);
      chooser.afterDraw();
      mocks.accountForFirstSend = { id: "acct-4425" };
      openedIntent().onSignedIn("acct-4425");
      closeChooser();

      expect(mocks.launchChat).not.toHaveBeenCalled();
      expect(chooser.isLaunchPending()).toBe(true);

      // Selected before its record arrives: nothing to start yet.
      shown = "agent-welcome-4426";
      chooser.afterDraw();
      expect(mocks.launchChat).not.toHaveBeenCalled();

      mocks.provisional.set("agent-welcome-4426", { phase: "awaiting_first_send", account_id: "" });
      chooser.afterDraw();
      chooser.afterDraw();

      expect(mocks.launchChat).toHaveBeenCalledExactlyOnceWith("agent-welcome-4426", "acct-4425", "", "agent");
      expect(chooser.isLaunchPending()).toBe(false);
      // An account is signed in now: no chooser for the welcome chat.
      expect(mocks.openProviderChooser).toHaveBeenCalledTimes(1);
    });

    it("starts nothing a held sign-in waits for while an intake is deciding the chat", () => {
      let shown: string | null = null;
      let isIntakeUnderWay = false;
      const chooser = rootChooser(
        () => shown,
        vi.fn(),
        () => isIntakeUnderWay,
      );
      chooser.afterDraw();
      openedIntent().onSignedIn("acct-4427");

      mocks.provisional.set("agent-intake-4428", { phase: "awaiting_first_send", account_id: "" });
      shown = "agent-intake-4428";
      isIntakeUnderWay = true;
      chooser.afterDraw();
      expect(mocks.launchChat).not.toHaveBeenCalled();

      isIntakeUnderWay = false;
      chooser.afterDraw();
      expect(mocks.launchChat).toHaveBeenCalledExactlyOnceWith("agent-intake-4428", "acct-4427", "", "agent");
    });
  });
});

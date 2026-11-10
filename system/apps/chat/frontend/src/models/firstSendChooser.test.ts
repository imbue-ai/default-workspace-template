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
  openProviderChooser: mocks.openProviderChooser,
}));

import { isAwaitingProvider, launchOnSignIn, makeRootFirstSendChooser } from "./firstSendChooser";

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
  mocks.openProviderChooser.mockClear();
});

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

describe("makeRootFirstSendChooser", () => {
  it("opens the chooser once for the chat on screen awaiting a provider, so a dismissal stands", () => {
    const makeRoom = vi.fn();
    const chooser = makeRootFirstSendChooser(() => "agent-empty-list-4410", makeRoom);

    chooser.openIfAwaitingProvider();
    chooser.openIfAwaitingProvider();

    expect(mocks.openProviderChooser).toHaveBeenCalledTimes(1);
  });

  it("opens nothing while nothing is on screen, or the chat on screen has an account to start on", () => {
    let shown: string | null = null;
    const chooser = makeRootFirstSendChooser(() => shown, vi.fn());
    chooser.openIfAwaitingProvider();

    shown = "agent-empty-list-4410";
    mocks.accountForFirstSend = { id: "acct-4411" };
    chooser.openIfAwaitingProvider();

    expect(mocks.openProviderChooser).not.toHaveBeenCalled();
  });

  it("opens again from the chat's notice after a dismissal", () => {
    const chooser = makeRootFirstSendChooser(() => "agent-empty-list-4410", vi.fn());
    chooser.openIfAwaitingProvider();

    chooser.open();

    expect(mocks.openProviderChooser).toHaveBeenCalledTimes(2);
  });

  it("launches the chat on screen when the sign-in lands, even when the selection moved while it ran", () => {
    let shown = "agent-empty-list-4410";
    const chooser = makeRootFirstSendChooser(() => shown, vi.fn());
    chooser.openIfAwaitingProvider();
    const intent = openedIntent();

    // The seeded welcome chat arrives and is selected while the user signs in.
    mocks.provisional.set("agent-welcome-4415", { phase: "awaiting_first_send", account_id: "" });
    shown = "agent-welcome-4415";
    chooser.openIfAwaitingProvider();
    intent.onSignedIn("acct-4416");

    expect(mocks.launchChat).toHaveBeenCalledExactlyOnceWith("agent-welcome-4415", "acct-4416", "", "agent");
  });

  it("makes the chat on screen a room when the room row is picked", () => {
    const makeRoom = vi.fn();
    const chooser = makeRootFirstSendChooser(() => "agent-empty-list-4410", makeRoom);
    chooser.open();

    openedIntent().onRoomChosen();

    expect(makeRoom).toHaveBeenCalledExactlyOnceWith("agent-empty-list-4410");
  });
});

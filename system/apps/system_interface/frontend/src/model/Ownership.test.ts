import { describe, expect, it } from "vitest";
import { desktopRecord, rosterParticipantRecord } from "../testing/records";
import type { OwnIdentity } from "./Presence";
import { desktopsOf, isLeftBehind, isOwnDesktop, leftBehindDesktops, ownDesktops, ownerEntryOf } from "./Ownership";

const home = desktopRecord("home");
const work = desktopRecord("work");
const alices = desktopRecord("alice", { owner: "user:user-alice-0001" });
const gone = desktopRecord("gone", { owner: "user:user-gone-0002" });
const josh = rosterParticipantRecord("user:user-josh-0001", { display_name: "Josh", is_owner: true });
const alice = rosterParticipantRecord("user:user-alice-0001", { display_name: "Alice" });
const roster = [josh, alice];

const signedInOwner: OwnIdentity = {
  owner: true,
  user_id: "user-josh-0001",
  email: "josh@example.com",
  participant: "user:user-josh-0001",
};
const anonymousOwner: OwnIdentity = { owner: true, user_id: null, email: null, participant: "owner" };
const visitingAlice: OwnIdentity = {
  owner: false,
  user_id: "user-alice-0001",
  email: "alice@example.com",
  participant: "user:user-alice-0001",
};

describe("isOwnDesktop and ownDesktops", () => {
  it("reads a desktop with no owner as the owner's under either key, and a keyed one as its owner's alone", () => {
    expect(isOwnDesktop(home, signedInOwner)).toBe(true);
    expect(isOwnDesktop(home, anonymousOwner)).toBe(true);
    expect(isOwnDesktop(home, visitingAlice)).toBe(false);
    expect(isOwnDesktop(alices, visitingAlice)).toBe(true);
    expect(isOwnDesktop(alices, signedInOwner)).toBe(false);
    expect(isOwnDesktop(alices, anonymousOwner)).toBe(false);
  });

  it("takes the owner's desktops as the viewer's until a heartbeat has said who the viewer is", () => {
    expect(isOwnDesktop(home, null)).toBe(true);
    expect(isOwnDesktop(alices, null)).toBe(false);
  });

  it("lists the viewer's own in the shell's order", () => {
    expect(ownDesktops([home, alices, work], signedInOwner).map((desktop) => desktop.id)).toEqual(["home", "work"]);
    expect(ownDesktops([home, alices, work], visitingAlice).map((desktop) => desktop.id)).toEqual(["alice"]);
  });
});

describe("desktopsOf, ownerEntryOf, and the desktops left behind", () => {
  it("gives the owner the keyless desktops and everyone else the ones carrying their key", () => {
    expect(desktopsOf(josh, [home, alices, work]).map((desktop) => desktop.id)).toEqual(["home", "work"]);
    expect(desktopsOf(alice, [home, alices, work]).map((desktop) => desktop.id)).toEqual(["alice"]);
  });

  it("names a desktop's owner off the roster, and none for one whose owner has left it", () => {
    expect(ownerEntryOf(home, roster)).toBe(josh);
    expect(ownerEntryOf(alices, roster)).toBe(alice);
    expect(ownerEntryOf(gone, roster)).toBeNull();
    expect(ownerEntryOf(home, [alice])).toBeNull();
    expect(isLeftBehind(gone, roster)).toBe(true);
    expect(isLeftBehind(alices, roster)).toBe(false);
    expect(isLeftBehind(home, [])).toBe(false);
  });

  it("shows the desktops left behind to the owner alone", () => {
    expect(leftBehindDesktops([home, alices, gone], roster, signedInOwner).map((desktop) => desktop.id)).toEqual([
      "gone",
    ]);
    expect(leftBehindDesktops([home, alices, gone], roster, null).map((desktop) => desktop.id)).toEqual(["gone"]);
    expect(leftBehindDesktops([home, alices, gone], roster, visitingAlice)).toEqual([]);
  });
});

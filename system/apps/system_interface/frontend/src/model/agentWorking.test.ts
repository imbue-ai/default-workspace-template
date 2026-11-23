import { describe, expect, it } from "vitest";

import { makeAgentWorkingSignal } from "./agentWorking";

function signal(): {
  told: boolean[];
  noteAvatar: (isWorking: boolean) => void;
  notePages: (isWorking: boolean) => void;
} {
  const told: boolean[] = [];
  return { told, ...makeAgentWorkingSignal((isWorking) => told.push(isWorking)) };
}

describe("makeAgentWorkingSignal", () => {
  it("says nothing while nobody has worked", () => {
    const { told, noteAvatar, notePages } = signal();

    noteAvatar(false);
    notePages(false);

    expect(told).toEqual([]);
  });

  it("counts an agent as working on the avatar's word alone, and on a page's alone", () => {
    const fromAvatar = signal();
    fromAvatar.noteAvatar(true);
    fromAvatar.noteAvatar(false);
    expect(fromAvatar.told).toEqual([true, false]);

    // An agent whose activity never reaches the avatar is still heard of from the page that shows it.
    const fromPages = signal();
    fromPages.notePages(true);
    fromPages.notePages(false);
    expect(fromPages.told).toEqual([true, false]);
  });

  it("keeps saying working until both have stopped, and tells each change once", () => {
    const { told, noteAvatar, notePages } = signal();

    notePages(true);
    noteAvatar(true);
    notePages(true);
    expect(told).toEqual([true]);

    notePages(false);
    expect(told).toEqual([true]);

    noteAvatar(false);
    noteAvatar(false);
    expect(told).toEqual([true, false]);

    noteAvatar(true);
    expect(told).toEqual([true, false, true]);
  });
});

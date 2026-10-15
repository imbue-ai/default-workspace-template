// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.hoisted(() => {
  globalThis.requestAnimationFrame ??= ((cb: FrameRequestCallback): number =>
    setTimeout(() => cb(0), 0) as unknown as number) as typeof globalThis.requestAnimationFrame;
});

const openChatPage = vi.hoisted(() => vi.fn());
vi.mock("../shell", () => ({ openChatPage, openSubagentView: vi.fn() }));

import m from "mithril";

import { SubagentView } from "./SubagentView";
import { framePageInShell } from "../shellConnectionFixture";

const ATTRS = { chatId: "agent-1", agentId: "agent-1", subagentSessionId: "session-1" };

/** What mithril rejects with for a non-2xx answer. */
function rejection(code: number, response: unknown, message = "null"): Error {
  return Object.assign(new Error(message), { code, response });
}

async function mounted(): Promise<HTMLElement> {
  const root = document.createElement("div");
  document.body.appendChild(root);
  m.mount(root, { view: () => m(SubagentView, ATTRS) });
  await new Promise((resolve) => setTimeout(resolve, 5));
  m.redraw.sync();
  return root;
}

describe("the side conversation view", () => {
  beforeEach(() => {
    document.body.innerHTML = "";
    openChatPage.mockClear();
    vi.restoreAllMocks();
    // jsdom has none; the transcript's scroll engine watches the list with one.
    vi.stubGlobal(
      "ResizeObserver",
      class {
        observe(): void {}
        unobserve(): void {}
        disconnect(): void {}
      },
    );
  });

  it("says a stale side conversation is gone and leads back to the chat", async () => {
    vi.spyOn(m, "request").mockRejectedValue(
      rejection(404, {
        detail: "Chat 'agent-1' has no agent 'agent-9'",
        user_facing_message: "This side conversation is no longer available.",
        kind: "side_conversation_gone",
      }),
    );
    const root = await mounted();

    expect(root.textContent).toContain("This side conversation is no longer available.");
    expect(root.textContent).not.toContain("agent-9");
    const back = Array.from(root.querySelectorAll("button")).find((b) => b.textContent === "Back to the chat");
    back!.click();
    expect(openChatPage).toHaveBeenCalledWith("agent-1");
  });

  it("says a load that failed couldn't be done, keeps the server text behind See details, and loads again", async () => {
    const shell = framePageInShell(true);
    const request = vi
      .spyOn(m, "request")
      .mockRejectedValue(rejection(500, { detail: "Could not read the session file" }));
    const root = await mounted();

    expect(root.querySelector(".message-list-error-text")?.textContent).toBe(
      "This conversation couldn't be loaded. Try again. If this persists, report a bug.",
    );
    expect(root.querySelector(".see-details-text")?.textContent).toContain("Could not read the session file");
    (root.querySelector(".report-bug") as HTMLElement).click();
    expect(shell.posted("shell:report-bug")).toHaveLength(1);
    shell.release();

    (root.querySelector(".message-list-reload") as HTMLElement).click();
    await new Promise((resolve) => setTimeout(resolve, 5));
    expect(request).toHaveBeenCalledTimes(2);
  });

  it("offers no Report a bug where the shell said Imbue Studio cannot open one", async () => {
    const shell = framePageInShell(false);
    vi.spyOn(m, "request").mockRejectedValue(rejection(500, { detail: "Could not read the session file" }));
    const root = await mounted();

    expect(root.querySelector(".message-list-error-text")?.textContent).toContain("If this persists, report a bug.");
    expect(root.querySelector(".report-bug")).toBeNull();
    shell.release();
  });
});

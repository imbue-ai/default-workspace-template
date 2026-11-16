// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";
import m from "mithril";
import type { MentionHandle } from "../models/Mentions";
import { insertMention, mentionDraftAt, mentionPopover } from "./MentionPopover";

const HANDLES: MentionHandle[] = [
  { handle: "Josh", display_name: "Josh", kind: "person", key: "user:josh", chat_id: null },
  { handle: "agent", display_name: "Atlas", kind: "workspace_agent", key: "workspace-agent", chat_id: null },
  { handle: "Main-chat", display_name: "Main chat", kind: "chat", key: null, chat_id: "agent-1" },
];

describe("mentionDraftAt", () => {
  it("reads the mention the caret follows, and none inside a word or after the mention", () => {
    expect(mentionDraftAt("hi @Jo", 6, null)).toEqual({ start: 3, query: "Jo", index: 0 });
    expect(mentionDraftAt("hi @", 4, null)).toEqual({ start: 3, query: "", index: 0 });
    expect(mentionDraftAt("mail me@example", 15, null)).toBeNull();
    expect(mentionDraftAt("hi @Josh there", 14, null)).toBeNull();
    expect(mentionDraftAt("hi @Josh there", 8, null)).toEqual({ start: 3, query: "Josh", index: 0 });
  });

  it("keeps the selected row while the same mention is being typed", () => {
    const previous = { start: 3, query: "J", index: 2 };
    expect(mentionDraftAt("hi @Jo", 6, previous)?.index).toBe(2);
    expect(mentionDraftAt("hi there @Jo", 12, previous)?.index).toBe(0);
  });
});

describe("insertMention", () => {
  it("replaces the draft with the handle and a space, and puts the caret after it", () => {
    expect(insertMention("ask @ag about it", { start: 4, query: "ag", index: 0 }, "agent")).toEqual({
      text: "ask @agent  about it",
      caret: 11,
    });
  });
});

describe("mentionPopover", () => {
  function render(node: m.Children): HTMLElement {
    const root = document.createElement("div");
    m.render(root, node);
    return root;
  }

  it("lists the rows with the selected one marked, and picks on a click", () => {
    const onPick = vi.fn();
    const root = render(mentionPopover({ rows: HANDLES, selectedIndex: 1, onPick, onHover: () => undefined }));

    const options = root.querySelectorAll("[role=option]");
    expect(options).toHaveLength(3);
    expect(options[1].getAttribute("aria-selected")).toBe("true");
    expect(options[1].textContent).toContain("@agent");
    expect(options[1].textContent).toContain("Atlas");
    (options[2] as HTMLElement).click();
    expect(onPick).toHaveBeenCalledWith(HANDLES[2]);
  });

  it("draws nothing with no rows", () => {
    expect(
      mentionPopover({ rows: [], selectedIndex: 0, onPick: () => undefined, onHover: () => undefined }),
    ).toBeNull();
  });
});

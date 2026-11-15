// @vitest-environment jsdom
import "../../testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";

import { afterEach, describe, expect, it, vi } from "vitest";

import m from "mithril";

import { avatarStateRecord } from "../../testing/records";
import { AgentCard } from "./AgentCard";
import type { AgentCardAttrs } from "./AgentCard";

function render(overrides: Partial<AgentCardAttrs> = {}): HTMLElement {
  const attrs: AgentCardAttrs = {
    anchor: null,
    onEnter: vi.fn(),
    onLeave: vi.fn(),
    avatar: avatarStateRecord({ status: { mood: "working", is_stale: false } }),
    workspaceName: "Atlas",
    workingOn: [{ chat_id: "agent-1", title: "Main chat", status: "working", kind: "agent" }],
    needsYou: [{ chat_id: "agent-2", title: "Deploy", status: "attention", kind: "agent" }],
    onChangeAvatar: vi.fn(),
    onFloat: vi.fn(),
    onOpenChat: vi.fn(),
    ...overrides,
  };
  return mountView(() => m(AgentCard, attrs));
}

afterEach(() => {
  unmountViews();
});

describe("AgentCard", () => {
  it("shows the workspace name, Working, the chats under Working on and Needs you, and the actions", () => {
    const onChangeAvatar = vi.fn();
    const onFloat = vi.fn();
    const onOpenChat = vi.fn();
    const root = render({ onChangeAvatar, onFloat, onOpenChat });
    expect(root.querySelector("[data-tray-card-name]")?.textContent).toBe("Atlas");
    expect(root.querySelector("[data-tray-card-status]")?.textContent).toBe("Working");
    expect(root.querySelector("[data-tray-card-chats='Working on'] [data-tray-card-chat]")?.textContent).toBe(
      "Main chat",
    );
    expect(root.querySelector("[data-tray-card-chats='Needs you'] [data-tray-card-chat]")?.textContent).toBe("Deploy");
    (root.querySelector("[data-tray-card-chat='agent-2']") as HTMLElement).click();
    expect(onOpenChat).toHaveBeenCalledWith("agent-2");
    (root.querySelector("[data-tray-card-change-avatar]") as HTMLElement).click();
    expect(onChangeAvatar).toHaveBeenCalledTimes(1);
    expect(root.querySelector("[data-tray-card-change-avatar]")?.textContent).toBe("Change avatar...");
    (root.querySelector("[data-tray-card-float]") as HTMLElement).click();
    expect(onFloat).toHaveBeenCalledTimes(1);
  });

  it("reads Idle with no chat lists when nothing is doing", () => {
    const root = render({ avatar: avatarStateRecord(), workingOn: [], needsYou: [] });
    expect(root.querySelector("[data-tray-card-status]")?.textContent).toBe("Idle");
    expect(root.querySelector("[data-tray-card-chats]")).toBeNull();
  });
});

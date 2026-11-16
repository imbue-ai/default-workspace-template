// @vitest-environment jsdom
import { beforeEach, describe, expect, it } from "vitest";
import m from "mithril";
import type { AssistantMessageEvent, UserMessageEvent } from "../models/Response";
import { setBlockExpanded } from "./expansion-state";
import { renderAssistantRun } from "./message-renderers";
import { cdAnswerOf, returnToRootPart, type ReturnToRootPart } from "./return-to-root-chip";

const HOOK: UserMessageEvent = {
  timestamp: "2026-01-01T00:00:00Z",
  type: "user_message",
  event_id: "evt-hook",
  source: "claude",
  role: "user",
  content: "Stop hook feedback:\n[[ -e .git ] || exit 2]: Return to the repo root ...",
  display: "return_to_root",
};

function cdEvent(text = ""): AssistantMessageEvent {
  return {
    timestamp: "2026-01-01T00:00:01Z",
    type: "assistant_message",
    event_id: "evt-cd",
    source: "claude",
    model: "m",
    text,
    tool_calls: [
      {
        tool_call_id: "call-cd",
        tool_name: "Bash",
        input_chars: 40,
        is_directory_change: true,
        action_verb: "ran",
        action_target: "cd /home/user/workspace",
      },
    ],
    stop_reason: null,
    usage: null,
    is_auth_error: false,
    is_api_error: false,
    api_error_kind: null,
    is_provider_fault: false,
  };
}

function landed(): ReturnToRootPart {
  const event = cdEvent();
  return { ...returnToRootPart(HOOK), cd: { event, call: event.tool_calls[0] } };
}

let root: HTMLElement;

function mount(part: ReturnToRootPart): void {
  m.render(root, m("div", renderAssistantRun([part], new Map(), "agent-x")));
}

function openPanel(part: ReturnToRootPart): Element {
  mount(part);
  root.querySelector<HTMLButtonElement>(".tool-chip.return-to-root-chip")!.click();
  mount(part);
  const panel = root.querySelector(".tool-chip-detail.return-to-root-detail");
  expect(panel).not.toBeNull();
  return panel!;
}

beforeEach(() => {
  setBlockExpanded("chip:return-to-root:evt-hook", false);
  root = document.createElement("div");
  document.body.appendChild(root);
});

describe("the return to the workspace folder's chip", () => {
  it("is one chip in the run's chip row", () => {
    mount(landed());
    expect([...root.querySelectorAll(".tool-chip .tool-chip-label")].map((el) => el.textContent)).toEqual([
      "Back to the workspace folder",
    ]);
  });

  it("opens on why it happened and the command the agent ran", () => {
    const text = openPanel(landed()).textContent ?? "";
    expect(text).toContain("went back to the workspace folder");
    expect(text).toContain("cd /home/user/workspace");
  });

  it("opens on the explanation alone before the cd has landed", () => {
    const text = openPanel(returnToRootPart(HOOK)).textContent ?? "";
    expect(text).toContain("went back to the workspace folder");
    expect(text).not.toContain("cd /home/user/workspace");
  });
});

describe("the agent's answer to the hook", () => {
  it("is the lone cd", () => {
    expect(cdAnswerOf(cdEvent())?.tool_call_id).toBe("call-cd");
  });

  it("is not a cd the agent also talks about", () => {
    expect(cdAnswerOf(cdEvent("Going back to the repo root."))).toBeNull();
  });
});

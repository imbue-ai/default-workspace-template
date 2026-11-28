// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import m from "mithril";
import type { AssistantMessageEvent, ToolResultEvent, TranscriptEvent, UserMessageEvent } from "../models/Response";
import { closeImageLightbox } from "../lightbox";
import { buildConversationRows, type RowDescriptor } from "./conversation-rows";

function userMsg(ts: string, content: string): UserMessageEvent {
  return { timestamp: ts, type: "user_message", event_id: `u-${ts}`, source: "test", role: "user", content };
}

function assistantMsg(ts: string, text: string, callIds: string[] = []): AssistantMessageEvent {
  return {
    timestamp: ts,
    type: "assistant_message",
    event_id: `a-${ts}`,
    source: "test",
    model: "m",
    text,
    tool_calls: callIds.map((id) => ({ tool_call_id: id, tool_name: "exec", input_chars: 40, action_note: id })),
    stop_reason: callIds.length > 0 ? "tool_use" : "end_turn",
    usage: null,
    is_auth_error: false,
    is_api_error: false,
    api_error_kind: null,
    is_provider_fault: false,
  };
}

function toolResult(ts: string, callId: string, over: Partial<ToolResultEvent> = {}): ToolResultEvent {
  return {
    timestamp: ts,
    type: "tool_result",
    event_id: `r-${callId}`,
    source: "test",
    tool_call_id: callId,
    tool_name: "exec",
    output_chars: 0,
    is_error: false,
    ...over,
  };
}

/** A tk lifecycle call and its result, which the timeline reads as a step transition. */
function tkCall(ts: string, callId: string, command: string, output: string): TranscriptEvent[] {
  return [
    {
      ...assistantMsg(ts, ""),
      event_id: `a-${callId}`,
      tool_calls: [
        { tool_call_id: callId, tool_name: "Bash", input_chars: 20, tk_command: command, display: "hidden" },
      ],
    },
    { ...toolResult(ts, callId), tk_stamp: output },
  ];
}

const GENERATED = { image_count: 1, is_generated_image: true } as const;

/** Every row mounted into one list, as the windowed list mounts them. */
function renderRows(rows: RowDescriptor[]): HTMLElement {
  const list = document.createElement("div");
  for (const row of rows) {
    const slot = document.createElement("div");
    m.render(slot, row.render());
    list.appendChild(slot);
  }
  document.body.appendChild(list);
  return list;
}

function render(events: TranscriptEvent[], isTailSettled = true): HTMLElement {
  return renderRows(buildConversationRows("chat-1", events, isTailSettled));
}

function images(list: HTMLElement): HTMLImageElement[] {
  return [...list.querySelectorAll<HTMLImageElement>(".generated-images img")];
}

afterEach(() => {
  closeImageLightbox();
  document.body.innerHTML = "";
});

describe("a generated image on the transcript", () => {
  it("shows inside a turn whose step is still folded, with the call that made it left folded in the step", () => {
    const list = render(
      [
        userMsg("t1", "draw me a fox"),
        ...tkCall(
          "t2",
          "c-s1",
          "tk start cod-step-aaa",
          "Updated cod-step-aaa -> in_progress\ntk-step cod-step-aaa title: Draw the fox",
        ),
        assistantMsg("t3", "", ["generate"]),
        toolResult("t3", "generate", GENERATED),
      ],
      /* isTailSettled */ false,
    );
    expect(list.querySelector(".pv-expanded")).toBeNull();
    expect(list.querySelector(".tool-chip")).toBeNull();
    expect(images(list).map((image) => image.getAttribute("src"))).toEqual([
      "/api/chats/chat-1/events/r-generate/images/0",
    ]);
    // It stands after the step it came from, on the stretch's line.
    const step = list.querySelector(".pv-tl-node--step")!;
    const shown = list.querySelector(".generated-images")!;
    expect(step.compareDocumentPosition(shown) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(shown.querySelector("[data-rail-node='box']")).not.toBeNull();
  });

  it("shows after the chip of the call that made it in a turn with no steps, above the reply", () => {
    const list = render([
      userMsg("t1", "draw me a fox"),
      assistantMsg("t2", "", ["look"]),
      toolResult("t2", "look"),
      assistantMsg("t3", "", ["generate"]),
      toolResult("t3", "generate", { image_count: 2, is_generated_image: true }),
      assistantMsg("t4", "Here is your fox."),
    ]);
    const pieces = [...list.querySelectorAll(".tool-chip-group, .generated-images, [data-machine-bubble]")];
    expect(pieces.map((piece) => piece.className.split(" ")[0])).toEqual([
      "tool-chip-group",
      "generated-images",
      "agent-prose",
    ]);
    // The call keeps its chip, beside the call before it.
    expect([...list.querySelectorAll(".tool-chip-label")].map((label) => label.textContent)).toEqual([
      "look",
      "generate",
    ]);
    expect(images(list).map((image) => [image.getAttribute("src"), image.alt])).toEqual([
      ["/api/chats/chat-1/events/r-generate/images/0", "Generated image (1 of 2)"],
      ["/api/chats/chat-1/events/r-generate/images/1", "Generated image (2 of 2)"],
    ]);
  });

  it("shows only once the result carrying it lands", () => {
    const pending: TranscriptEvent[] = [userMsg("t1", "draw me a fox"), assistantMsg("t2", "", ["generate"])];
    expect(images(render(pending, false))).toEqual([]);
    expect(images(render([...pending, toolResult("t3", "generate", GENERATED)], false))).toHaveLength(1);
  });

  it("opens full-screen on a click", () => {
    const list = render([
      userMsg("t1", "draw me a fox"),
      assistantMsg("t2", "", ["generate"]),
      toolResult("t2", "generate", GENERATED),
    ]);
    list.querySelector<HTMLButtonElement>(".tool-result-image-open")!.click();
    const enlarged = document.querySelector<HTMLImageElement>(".image-lightbox-img")!;
    expect(enlarged.getAttribute("src")).toBe("/api/chats/chat-1/events/r-generate/images/0");
    expect(document.querySelector(".image-lightbox-title")?.textContent).toBe("Generated image");
  });

  it("holds its space until it loads, and says so in its place when it cannot be fetched", () => {
    const events: TranscriptEvent[] = [
      userMsg("t1", "draw me two foxes"),
      assistantMsg("t2", "", ["generate"]),
      toolResult("t2", "generate", { image_count: 2, is_generated_image: true }),
    ];
    const rows = buildConversationRows("chat-1", events, true);
    const slot = document.createElement("div");
    const draw = (): void =>
      m.render(
        slot,
        rows.map((row) => row.render()),
      );
    draw();
    const [first, second] = [...slot.querySelectorAll<HTMLImageElement>(".tool-result-image")];
    expect(first.classList.contains("is-loading")).toBe(true);

    first.dispatchEvent(new Event("load"));
    second.dispatchEvent(new Event("error"));
    draw();
    expect(slot.querySelectorAll(".tool-result-image")).toHaveLength(1);
    expect(slot.querySelector(".tool-result-image")!.classList.contains("is-loading")).toBe(false);
    expect(slot.querySelector(".tool-result-image-error")?.textContent).toBe("Couldn't load this image.");
  });
});

describe("an image the agent only looked at", () => {
  it("stays in its call's chip, shown only when the chip is opened", () => {
    const list = render([
      userMsg("t1", "what is in this screenshot?"),
      assistantMsg("t2", "", ["view"]),
      toolResult("t2", "view", { image_count: 1 }),
    ]);
    expect(list.querySelector(".generated-images")).toBeNull();
    expect(list.querySelector("img")).toBeNull();
  });
});

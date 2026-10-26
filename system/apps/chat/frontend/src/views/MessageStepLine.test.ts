import { describe, expect, it } from "vitest";
import type { MessageStep } from "../models/Chats";
import { findMessageStep, messageStepStateLabel } from "./MessageStepLine";

function step(overrides: Partial<MessageStep>): MessageStep {
  return {
    step_id: "wor-step-aaaa",
    message_id: "message-1",
    message_text: "Add a dark mode",
    title: "Add a dark mode",
    status: "open",
    summary: "",
    created_at: "2026-10-26T21:33:00.000000Z",
    ...overrides,
  };
}

describe("findMessageStep", () => {
  it("finds a bubble's step by the message's words, ignoring outer whitespace", () => {
    const steps = [step({ step_id: "wor-step-aaaa" })];
    expect(findMessageStep(steps, "  Add a dark mode\n")?.step_id).toBe("wor-step-aaaa");
  });

  it("takes the newest step when the same words were sent twice", () => {
    const steps = [step({ step_id: "wor-step-old" }), step({ step_id: "wor-step-new" })];
    expect(findMessageStep(steps, "Add a dark mode")?.step_id).toBe("wor-step-new");
  });

  it("prefers the step with the bubble's send-time id", () => {
    const steps = [
      step({ step_id: "wor-step-old", message_id: "message-1" }),
      step({ step_id: "wor-step-new", message_id: "message-2" }),
    ];
    expect(findMessageStep(steps, "Add a dark mode", "message-1")?.step_id).toBe("wor-step-old");
  });

  it("finds nothing for a message no step was made for", () => {
    expect(findMessageStep([step({})], "Something else")).toBeNull();
  });
});

describe("messageStepStateLabel", () => {
  it("reads to do, in progress and done", () => {
    expect(messageStepStateLabel(step({}))).toBe("To do");
    expect(messageStepStateLabel(step({ status: "in_progress" }))).toBe("In progress");
    expect(messageStepStateLabel(step({ status: "closed", summary: "Added it" }))).toBe("Done");
  });
});

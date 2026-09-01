import { describe, expect, it } from "vitest";
import {
  classifyUserMessage,
  isHiddenUserMessage,
  isNonBoundaryUserMessage,
  isSkillExpansionUserMessage,
  isStatusUserMessage,
  isSystemChipUserMessage,
  resolutionOf,
  resolutionRequestIdOf,
} from "./message-classification";

import { UserMessageKind } from "./message-kinds";

// The DETECTION cases (which content becomes which decision) live backend-side now, in
// `harnesses/message_display_test.py` -- the detector table moved there. These tests pin
// the frontend's remaining job: mapping the wire's `display` fields onto the kind
// catalogue, with zero content sniffing.

describe("classifyUserMessage", () => {
  it("treats a message with no display decision as UserPrompt with the content as body", () => {
    const c = classifyUserMessage({ content: "please rebase onto main" });
    expect(c.kind).toBe(UserMessageKind.UserPrompt);
    expect(c.body).toBe("please rebase onto main");
    expect(c.label).toBeNull();
  });

  it("maps display: hidden to Hidden", () => {
    expect(classifyUserMessage({ content: "/welcome", display: "hidden" }).kind).toBe(UserMessageKind.Hidden);
  });

  it("maps display: chip to SystemChip with the backend's label", () => {
    const c = classifyUserMessage({
      content: "Stop hook feedback:\nlint failed",
      display: "chip",
      display_label: "Stop hook feedback",
    });
    expect(c.kind).toBe(UserMessageKind.SystemChip);
    expect(c.label).toBe("Stop hook feedback");
    expect(c.body).toBe("Stop hook feedback:\nlint failed");
  });

  it("prefers the backend's display_body (a stripped wrapper sentinel) for the chip body", () => {
    const c = classifyUserMessage({
      content: "<agentic-browser-fleet>Browser foo-1 is free</agentic-browser-fleet>",
      display: "chip",
      display_label: "Browser fleet",
      display_body: "Browser foo-1 is free",
    });
    expect(c.body).toBe("Browser foo-1 is free");
  });

  it("maps display: skill_expansion to SkillExpansion with the skill name", () => {
    const c = classifyUserMessage({
      content: "Base directory for this skill: /x/skills/deep-research/",
      display: "skill_expansion",
      display_label: "deep-research",
    });
    expect(c.kind).toBe(UserMessageKind.SkillExpansion);
    expect(c.label).toBe("deep-research");
  });

  it("maps an uncorrelated permission_resolution to UserPrompt (the walk owns suppression)", () => {
    const c = classifyUserMessage({
      content: "Your permission request for GitHub was granted.",
      display: "permission_resolution",
    });
    expect(c.kind).toBe(UserMessageKind.UserPrompt);
  });

  it("maps display: status to StatusMessage with content as body", () => {
    const c = classifyUserMessage({
      content: "Context was compacted",
      display: "status",
    });
    expect(c.kind).toBe(UserMessageKind.StatusMessage);
    expect(c.body).toBe("Context was compacted");
    expect(c.label).toBeNull();
  });
});

describe("semantic helpers", () => {
  it("isNonBoundaryUserMessage is true for non-boundary kinds and false for prompt and status boundaries", () => {
    expect(isNonBoundaryUserMessage({ content: "x", display: "chip", display_label: "Stop hook feedback" })).toBe(
      true,
    );
    expect(isNonBoundaryUserMessage({ content: "x", display: "status" })).toBe(false);
    expect(isNonBoundaryUserMessage({ content: "x", display: "skill_expansion" })).toBe(true);
    expect(isNonBoundaryUserMessage({ content: "/welcome", display: "hidden" })).toBe(true);
    expect(isNonBoundaryUserMessage({ content: "a normal message" })).toBe(false);
  });

  it("isSystemChipUserMessage is true only for the collapsed-chip kind", () => {
    expect(isSystemChipUserMessage({ content: "x", display: "chip", display_label: "Background task" })).toBe(true);
    expect(isSystemChipUserMessage({ content: "x", display: "status" })).toBe(false);
    expect(isSystemChipUserMessage({ content: "x", display: "skill_expansion" })).toBe(false);
    expect(isSystemChipUserMessage({ content: "/welcome", display: "hidden" })).toBe(false);
    expect(isSystemChipUserMessage({ content: "a normal message" })).toBe(false);
  });

  it("isStatusUserMessage is true only for status kind", () => {
    expect(isStatusUserMessage({ content: "Context was compacted", display: "status" })).toBe(true);
    expect(isStatusUserMessage({ content: "x", display: "chip", display_label: "Background task" })).toBe(false);
    expect(isStatusUserMessage({ content: "a normal message" })).toBe(false);
  });

  it("isHiddenUserMessage covers hidden and relocated kinds (no user-rail row)", () => {
    expect(isHiddenUserMessage({ content: "/welcome", display: "hidden" })).toBe(true);
    expect(isHiddenUserMessage({ content: "x", display: "skill_expansion" })).toBe(true);
    expect(isHiddenUserMessage({ content: "x", display: "status" })).toBe(false);
    expect(isHiddenUserMessage({ content: "x", display: "chip", display_label: "Stop hook feedback" })).toBe(false);
    expect(isHiddenUserMessage({ content: "a normal message" })).toBe(false);
  });

  it("isSkillExpansionUserMessage matches only skill expansions", () => {
    expect(isSkillExpansionUserMessage({ content: "x", display: "skill_expansion" })).toBe(true);

    expect(isSkillExpansionUserMessage({ content: "/welcome", display: "hidden" })).toBe(false);
  });
});

describe("resolutionOf", () => {
  it("reads the verdict off a permission_resolution and nothing else", () => {
    expect(resolutionOf({ display: "permission_resolution", resolution: "granted" })).toBe("granted");
    expect(resolutionOf({ display: "permission_resolution", resolution: "denied" })).toBe("denied");
    expect(resolutionOf({ display: "permission_resolution", resolution: "error" })).toBe("error");
    expect(resolutionOf({ display: "hidden" })).toBeNull();
    expect(resolutionOf({})).toBeNull();
  });
});

describe("resolutionRequestIdOf", () => {
  it("reads the request id off a permission_resolution and nothing else", () => {
    expect(resolutionRequestIdOf({ display: "permission_resolution", request_id: "req-1" })).toBe("req-1");
    expect(resolutionRequestIdOf({ display: "hidden", request_id: "req-1" })).toBeNull();
    expect(resolutionRequestIdOf({})).toBeNull();
  });

  it("is null for a resolution recorded before request-id embedding shipped", () => {
    expect(resolutionRequestIdOf({ display: "permission_resolution" })).toBeNull();
  });
});

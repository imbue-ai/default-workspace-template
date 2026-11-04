// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";

import { chatUrlOf } from "./chatLink";

function setChatLabel(label: string): void {
  const meta = document.createElement("meta");
  meta.name = "routines-chat-label";
  meta.content = label;
  document.head.appendChild(meta);
}

afterEach(() => {
  document.head.querySelectorAll('meta[name="routines-chat-label"]').forEach((meta) => meta.remove());
});

describe("chatUrlOf", () => {
  it("puts the chat app's minted label on this workspace's coordinate, with the chat selected", () => {
    setChatLabel("chat-x7k9q2w1");
    expect(chatUrlOf("agent-1", "routines-ab12cd34.host-1a2b3c4d.localhost", "http:")).toBe(
      "http://chat-x7k9q2w1.host-1a2b3c4d.localhost/?chat=agent-1",
    );
  });

  it("falls back to the chat app's name while it has no label", () => {
    expect(chatUrlOf("agent-1", "routines-ab12cd34.host-1a2b3c4d.localhost", "http:")).toBe(
      "http://chat.host-1a2b3c4d.localhost/?chat=agent-1",
    );
  });
});

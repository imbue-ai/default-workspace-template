import { describe, expect, it, vi } from "vitest";

// apiUrl reads a <meta> tag via document, absent in the node test environment.
vi.mock("./base-path", () => ({ apiUrl: (path: string) => path }));
// lightbox touches the DOM imperatively; markdown.ts only needs its export to exist.
vi.mock("./lightbox", () => ({ openImageLightbox: vi.fn() }));

import { chatImageUrl } from "./markdown";

describe("chatImageUrl", () => {
  it("routes an absolute on-disk path through the per-message change-checking endpoint", () => {
    expect(chatImageUrl("/mngr/code/runtime/chat-images/chart.png", "event-1")).toBe(
      "/api/chat-images/event-1/mngr/code/runtime/chat-images/chart.png",
    );
  });

  it("percent-encodes path segments and the event id", () => {
    expect(chatImageUrl("/tmp/my chart.png", "id/with?chars")).toBe(
      "/api/chat-images/id%2Fwith%3Fchars/tmp/my%20chart.png",
    );
  });
});

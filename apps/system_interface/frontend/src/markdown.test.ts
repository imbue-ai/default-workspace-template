import { describe, expect, it, vi } from "vitest";

// lightbox touches the DOM imperatively; markdown.ts only needs its export to exist.
vi.mock("./lightbox", () => ({ openImageLightbox: vi.fn() }));

import { requestedAtUrl } from "./markdown";

describe("requestedAtUrl", () => {
  it("appends the per-message post time to an absolute on-disk path", () => {
    expect(requestedAtUrl("/home/user/workspace/runtime/chat-images/chart.png", "2026-07-24T00:00:00Z")).toBe(
      "/home/user/workspace/runtime/chat-images/chart.png?requested_at=2026-07-24T00%3A00%3A00Z",
    );
  });

  it("works for download-link paths too", () => {
    expect(requestedAtUrl("/home/user/workspace/runtime/chat-files/report.pdf", "ts-1")).toBe(
      "/home/user/workspace/runtime/chat-files/report.pdf?requested_at=ts-1",
    );
  });

  it("leaves external URLs untouched", () => {
    expect(requestedAtUrl("https://example.com/pic.png", "ts-1")).toBeNull();
    expect(requestedAtUrl("//cdn.example.com/pic.png", "ts-1")).toBeNull();
  });

  it("leaves app routes untouched", () => {
    expect(requestedAtUrl("/api/uploads/x", "ts-1")).toBeNull();
  });

  it("does not double-append when a query string is already present", () => {
    expect(requestedAtUrl("/x/chart.png?v=2", "ts-1")).toBeNull();
  });
});
